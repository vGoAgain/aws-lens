import os
import json
import logging
import boto3
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
from botocore.exceptions import ClientError, NoCredentialsError
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key-change-in-prod")


# ─── DB URL builder ───────────────────────────────────────────────────────────

def get_secret(secret_name: str, region: str = "us-east-1") -> dict:
    client = boto3.session.Session().client("secretsmanager", region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
        return json.loads(resp["SecretString"])
    except ClientError as e:
        logger.error(f"Secrets Manager error: {e}")
        raise


def build_db_url() -> str:
    secret_name = os.environ.get("DB_SECRET_NAME")
    if secret_name:
        region = os.environ.get("AWS_REGION", "us-east-1")
        creds = get_secret(secret_name, region)
        host = creds.get("host", os.environ.get("DB_HOST", "localhost"))
        port = creds.get("port", os.environ.get("DB_PORT", "5432"))
        dbname = creds.get("dbname", os.environ.get("DB_NAME", "awslensdb"))
        user = creds["username"]
        password = creds["password"]
    else:
        host = os.environ.get("DB_HOST", "localhost")
        port = os.environ.get("DB_PORT", "5432")
        dbname = os.environ.get("DB_NAME", "awslensdb")
        user = os.environ.get("DB_USER", "postgres")
        password = os.environ.get("DB_PASSWORD", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


app.config["SQLALCHEMY_DATABASE_URI"] = build_db_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# ─── Models ───────────────────────────────────────────────────────────────────

class ScanSession(db.Model):
    __tablename__ = "scan_sessions"

    id = db.Column(db.Integer, primary_key=True)
    scanned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    aws_account_id = db.Column(db.String(30))
    aws_region = db.Column(db.String(50))
    alias = db.Column(db.String(150))          # optional label given by user
    total_estimated_cost = db.Column(db.Numeric(12, 4), default=0)
    services = db.relationship("ServiceEntry", back_populates="session", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "scanned_at": self.scanned_at.isoformat(),
            "aws_account_id": self.aws_account_id,
            "aws_region": self.aws_region,
            "alias": self.alias,
            "total_estimated_cost": float(self.total_estimated_cost or 0),
            "service_count": len(self.services),
        }


class ServiceEntry(db.Model):
    __tablename__ = "service_entries"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("scan_sessions.id"), nullable=False)
    service_name = db.Column(db.String(100), nullable=False)
    resource_count = db.Column(db.Integer, default=0)
    is_chargeable = db.Column(db.Boolean, default=False)
    estimated_monthly_cost = db.Column(db.Numeric(12, 4), default=0)
    details = db.Column(db.Text)   # JSON string with resource details
    session = db.relationship("ScanSession", back_populates="services")

    def to_dict(self):
        return {
            "id": self.id,
            "service_name": self.service_name,
            "resource_count": self.resource_count,
            "is_chargeable": self.is_chargeable,
            "estimated_monthly_cost": float(self.estimated_monthly_cost or 0),
            "details": json.loads(self.details) if self.details else [],
        }


# ─── AWS Scanner ──────────────────────────────────────────────────────────────

class AWSScanner:
    """Scans an AWS account for active, chargeable services."""

    # (service_name, free_tier_threshold_note, approx_monthly_cost_per_unit, unit)
    COST_MAP = {
        "EC2 Instances":        (True,  0.10,  "per instance/hr ~$73/mo"),
        "RDS Instances":        (True,  0.115, "per instance/hr ~$83/mo"),
        "S3 Buckets":           (True,  0.023, "per GB stored"),
        "Lambda Functions":     (False, 0.0,   "free tier generous; pay per invocation"),
        "ECS Clusters":         (True,  0.0,   "cluster free; tasks billed via EC2/Fargate"),
        "EKS Clusters":         (True,  0.10,  "per cluster/hr ~$73/mo"),
        "ElastiCache Clusters": (True,  0.068, "per node/hr ~$49/mo"),
        "DynamoDB Tables":      (False, 0.25,  "per million WCU (on-demand)"),
        "CloudFront Distros":   (False, 0.0085,"per 10K HTTPS requests"),
        "NAT Gateways":         (True,  0.045, "per hr ~$33/mo + data"),
        "Load Balancers":       (True,  0.008, "per LCU/hr"),
        "Elastic IPs":          (True,  0.005, "per unattached IP/hr"),
        "Secrets Manager":      (True,  0.40,  "per secret/mo"),
        "SNS Topics":           (False, 0.50,  "per million publishes"),
        "SQS Queues":           (False, 0.40,  "per million requests"),
        "Kinesis Streams":      (True,  0.015, "per shard/hr ~$11/mo"),
        "Glue Jobs":            (True,  0.44,  "per DPU-hr"),
        "Redshift Clusters":    (True,  0.25,  "per node/hr ~$180/mo"),
        "OpenSearch Domains":   (True,  0.096, "per instance/hr"),
        "CodeBuild Projects":   (False, 0.005, "per build-minute"),
    }

    def __init__(self, access_key: str, secret_key: str, region: str = "us-east-1",
                 session_token: str = None):
        kwargs = dict(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        if session_token:
            kwargs["aws_session_token"] = session_token
        self.session = boto3.Session(**kwargs)
        self.region = region

    def get_account_id(self) -> str:
        try:
            return self.session.client("sts").get_caller_identity()["Account"]
        except Exception:
            return "unknown"

    def _scan_ec2(self) -> dict:
        try:
            ec2 = self.session.client("ec2")
            resp = ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped"]}])
            instances = [i for r in resp["Reservations"] for i in r["Instances"]]
            running = [i for i in instances if i["State"]["Name"] == "running"]
            details = [{"id": i["InstanceId"], "type": i["InstanceType"], "state": i["State"]["Name"]} for i in instances[:20]]
            cost = len(running) * 73.0
            return {"count": len(instances), "cost": cost, "details": details, "chargeable": len(running) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_rds(self) -> dict:
        try:
            rds = self.session.client("rds")
            dbs = rds.describe_db_instances()["DBInstances"]
            running = [d for d in dbs if d["DBInstanceStatus"] == "available"]
            details = [{"id": d["DBInstanceIdentifier"], "engine": d["Engine"], "class": d["DBInstanceClass"], "status": d["DBInstanceStatus"]} for d in dbs[:20]]
            cost = len(running) * 83.0
            return {"count": len(dbs), "cost": cost, "details": details, "chargeable": len(running) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_s3(self) -> dict:
        try:
            s3 = self.session.client("s3")
            buckets = s3.list_buckets()["Buckets"]
            details = [{"name": b["Name"], "created": b["CreationDate"].isoformat()} for b in buckets[:20]]
            cost = len(buckets) * 0.5   # rough estimate; actual depends on storage
            return {"count": len(buckets), "cost": cost, "details": details, "chargeable": len(buckets) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_lambda(self) -> dict:
        try:
            lmb = self.session.client("lambda")
            fns = []
            paginator = lmb.get_paginator("list_functions")
            for page in paginator.paginate():
                fns.extend(page["Functions"])
            details = [{"name": f["FunctionName"], "runtime": f.get("Runtime", "N/A"), "memory": f["MemorySize"]} for f in fns[:20]]
            return {"count": len(fns), "cost": 0.0, "details": details, "chargeable": False}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_ecs(self) -> dict:
        try:
            ecs = self.session.client("ecs")
            clusters = ecs.list_clusters()["clusterArns"]
            details = [{"arn": c.split("/")[-1]} for c in clusters[:20]]
            return {"count": len(clusters), "cost": 0.0, "details": details, "chargeable": len(clusters) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_eks(self) -> dict:
        try:
            eks = self.session.client("eks")
            clusters = eks.list_clusters()["clusters"]
            details = [{"name": c} for c in clusters[:20]]
            cost = len(clusters) * 73.0
            return {"count": len(clusters), "cost": cost, "details": details, "chargeable": len(clusters) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_elasticache(self) -> dict:
        try:
            ec = self.session.client("elasticache")
            clusters = ec.describe_cache_clusters()["CacheClusters"]
            running = [c for c in clusters if c["CacheClusterStatus"] == "available"]
            details = [{"id": c["CacheClusterId"], "engine": c["Engine"], "status": c["CacheClusterStatus"]} for c in clusters[:20]]
            cost = len(running) * 49.0
            return {"count": len(clusters), "cost": cost, "details": details, "chargeable": len(running) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_dynamodb(self) -> dict:
        try:
            ddb = self.session.client("dynamodb")
            tables = ddb.list_tables()["TableNames"]
            details = [{"name": t} for t in tables[:20]]
            return {"count": len(tables), "cost": 0.0, "details": details, "chargeable": len(tables) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_cloudfront(self) -> dict:
        try:
            cf = self.session.client("cloudfront")
            distros = cf.list_distributions()["DistributionList"].get("Items", [])
            details = [{"id": d["Id"], "domain": d["DomainName"], "status": d["Status"]} for d in distros[:20]]
            return {"count": len(distros), "cost": 0.0, "details": details, "chargeable": len(distros) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_nat_gateways(self) -> dict:
        try:
            ec2 = self.session.client("ec2")
            nats = ec2.describe_nat_gateways(Filters=[{"Name": "state", "Values": ["available"]}])["NatGateways"]
            details = [{"id": n["NatGatewayId"], "state": n["State"], "vpc": n["VpcId"]} for n in nats[:20]]
            cost = len(nats) * 33.0
            return {"count": len(nats), "cost": cost, "details": details, "chargeable": len(nats) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_load_balancers(self) -> dict:
        try:
            elb = self.session.client("elbv2")
            lbs = elb.describe_load_balancers()["LoadBalancers"]
            details = [{"name": l["LoadBalancerName"], "type": l["Type"], "state": l["State"]["Code"]} for l in lbs[:20]]
            cost = len(lbs) * 18.0
            return {"count": len(lbs), "cost": cost, "details": details, "chargeable": len(lbs) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_elastic_ips(self) -> dict:
        try:
            ec2 = self.session.client("ec2")
            eips = ec2.describe_addresses()["Addresses"]
            unattached = [e for e in eips if "AssociationId" not in e]
            details = [{"ip": e.get("PublicIp"), "attached": "AssociationId" in e} for e in eips[:20]]
            cost = len(unattached) * 3.6
            return {"count": len(eips), "cost": cost, "details": details, "chargeable": len(unattached) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_secrets_manager(self) -> dict:
        try:
            sm = self.session.client("secretsmanager")
            secrets = sm.list_secrets()["SecretList"]
            details = [{"name": s["Name"]} for s in secrets[:20]]
            cost = len(secrets) * 0.40
            return {"count": len(secrets), "cost": cost, "details": details, "chargeable": len(secrets) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_sns(self) -> dict:
        try:
            sns = self.session.client("sns")
            topics = sns.list_topics()["Topics"]
            details = [{"arn": t["TopicArn"].split(":")[-1]} for t in topics[:20]]
            return {"count": len(topics), "cost": 0.0, "details": details, "chargeable": len(topics) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_sqs(self) -> dict:
        try:
            sqs = self.session.client("sqs")
            queues = sqs.list_queues().get("QueueUrls", [])
            details = [{"url": q.split("/")[-1]} for q in queues[:20]]
            return {"count": len(queues), "cost": 0.0, "details": details, "chargeable": len(queues) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def _scan_redshift(self) -> dict:
        try:
            rs = self.session.client("redshift")
            clusters = rs.describe_clusters()["Clusters"]
            details = [{"id": c["ClusterIdentifier"], "status": c["ClusterStatus"]} for c in clusters[:20]]
            cost = len(clusters) * 180.0
            return {"count": len(clusters), "cost": cost, "details": details, "chargeable": len(clusters) > 0}
        except Exception as e:
            return {"count": 0, "cost": 0, "details": [], "chargeable": False, "error": str(e)}

    def scan_all(self) -> list[dict]:
        scanners = [
            ("EC2 Instances",        self._scan_ec2),
            ("RDS Instances",        self._scan_rds),
            ("S3 Buckets",           self._scan_s3),
            ("Lambda Functions",     self._scan_lambda),
            ("ECS Clusters",         self._scan_ecs),
            ("EKS Clusters",         self._scan_eks),
            ("ElastiCache Clusters", self._scan_elasticache),
            ("DynamoDB Tables",      self._scan_dynamodb),
            ("CloudFront Distros",   self._scan_cloudfront),
            ("NAT Gateways",         self._scan_nat_gateways),
            ("Load Balancers",       self._scan_load_balancers),
            ("Elastic IPs",          self._scan_elastic_ips),
            ("Secrets Manager",      self._scan_secrets_manager),
            ("SNS Topics",           self._scan_sns),
            ("SQS Queues",           self._scan_sqs),
            ("Redshift Clusters",    self._scan_redshift),
        ]
        results = []
        for name, fn in scanners:
            logger.info(f"Scanning {name}…")
            data = fn()
            data["service_name"] = name
            results.append(data)
        return results


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scan", methods=["POST"])
def scan():
    access_key    = request.form.get("access_key", "").strip()
    secret_key    = request.form.get("secret_key", "").strip()
    session_token = request.form.get("session_token", "").strip() or None
    region        = request.form.get("region", "us-east-1").strip()
    alias         = request.form.get("alias", "").strip() or None

    if not access_key or not secret_key:
        flash("Access Key and Secret Key are required.", "error")
        return redirect(url_for("index"))

    try:
        scanner = AWSScanner(access_key, secret_key, region, session_token)
        account_id = scanner.get_account_id()
        raw_results = scanner.scan_all()
    except NoCredentialsError:
        flash("Invalid AWS credentials — please check your keys.", "error")
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"AWS error: {e}", "error")
        return redirect(url_for("index"))

    # Filter services that have at least one resource
    active = [r for r in raw_results if r.get("count", 0) > 0]
    total_cost = sum(r.get("cost", 0) for r in active)

    # Persist to DB
    session_obj = ScanSession(
        aws_account_id=account_id,
        aws_region=region,
        alias=alias,
        total_estimated_cost=total_cost,
    )
    db.session.add(session_obj)
    db.session.flush()

    for r in active:
        entry = ServiceEntry(
            session_id=session_obj.id,
            service_name=r["service_name"],
            resource_count=r.get("count", 0),
            is_chargeable=r.get("chargeable", False),
            estimated_monthly_cost=r.get("cost", 0),
            details=json.dumps(r.get("details", [])),
        )
        db.session.add(entry)

    db.session.commit()
    return redirect(url_for("result", session_id=session_obj.id))


@app.route("/result/<int:session_id>")
def result(session_id):
    sess = ScanSession.query.get_or_404(session_id)
    services = ServiceEntry.query.filter_by(session_id=session_id).order_by(
        ServiceEntry.estimated_monthly_cost.desc()
    ).all()
    chargeable = [s for s in services if s.is_chargeable]
    free = [s for s in services if not s.is_chargeable]
    return render_template("result.html", sess=sess, services=services,
                           chargeable=chargeable, free=free)


@app.route("/history")
def history():
    date_from = request.args.get("date_from", "")
    date_to   = request.args.get("date_to", "")

    query = ScanSession.query
    if date_from:
        try:
            query = query.filter(ScanSession.scanned_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            # include the whole end day
            end = datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
            query = query.filter(ScanSession.scanned_at <= end)
        except ValueError:
            pass

    sessions = query.order_by(ScanSession.scanned_at.desc()).all()
    return render_template("history.html", sessions=sessions,
                           date_from=date_from, date_to=date_to)


@app.route("/history/<int:session_id>")
def history_detail(session_id):
    return redirect(url_for("result", session_id=session_id))


@app.route("/history/<int:session_id>/delete", methods=["POST"])
def delete_session(session_id):
    sess = ScanSession.query.get_or_404(session_id)
    db.session.delete(sess)
    db.session.commit()
    flash("Scan session deleted.", "success")
    return redirect(url_for("history"))


@app.route("/api/history")
def api_history():
    sessions = ScanSession.query.order_by(ScanSession.scanned_at.desc()).limit(100).all()
    return jsonify([s.to_dict() for s in sessions])


@app.route("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "healthy", "db": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "unhealthy", "db": str(e)}), 500


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=5000, debug=debug)
