# AWS Lens

A two-tier web application that scans an AWS account for active, chargeable services,
estimates monthly costs, and persists every scan to PostgreSQL for historical analysis.

| Layer    | Technology                                      |
|----------|-------------------------------------------------|
| UI / App | Python 3.12 · Flask 3 · Gunicorn                |
| Database | PostgreSQL 16 (local) / Amazon RDS (production) |
| Secrets  | AWS Secrets Manager                             |
| Registry | Amazon ECR → ECS                                |

---

## Features

- **Multi-service scan** — EC2, RDS, S3, Lambda, ECS, EKS, ElastiCache, DynamoDB,
  CloudFront, NAT Gateways, Load Balancers, Elastic IPs, Secrets Manager, SNS, SQS, Redshift
- **Chargeable vs free-tier** classification per service
- **Estimated monthly cost** per service and session total
- **Resource detail drill-down** (IDs, types, statuses)
- **Persistent history** — every scan is logged to PostgreSQL
- **Date-range search** across scan history
- Credentials are used in-memory only — never persisted

---

## Project layout

```
aws-lens/
├── app/
│   ├── app.py               # Flask app, AWSScanner, SQLAlchemy models
│   ├── requirements.txt
│   └── templates/
│       ├── base.html
│       ├── index.html       # Scan input form
│       ├── result.html      # Scan results
│       └── history.html     # Date-range search history
├── scripts/
│   ├── deploy.sh            # ECR build & push
│   └── iam-task-policy.json # ECS Task Role IAM policy
├── Dockerfile               # Multi-stage build
├── docker-compose.yml       # Local dev stack
├── .env.example
└── README.md
```

---

## Quick start (local)

```bash
cp .env.example .env
docker compose up --build
# → http://localhost:5000
```

---

## Environment variables

| Variable         | Context     | Description                                                  |
|------------------|-------------|--------------------------------------------------------------|
| `FLASK_SECRET_KEY` | Both      | Session signing key                                          |
| `DB_SECRET_NAME` | AWS only    | Secrets Manager secret name — **overrides all DB_\* vars**  |
| `AWS_REGION`     | AWS only    | e.g. `us-east-1`                                             |
| `DB_HOST`        | Local dev   | Postgres host (docker-compose: `db`)                         |
| `DB_PORT`        | Local dev   | Default `5432`                                               |
| `DB_NAME`        | Local dev   | Default `awslensdb`                                          |
| `DB_USER`        | Local dev   | Default `postgres`                                           |
| `DB_PASSWORD`    | Local dev   | Default `postgres`                                           |

---

## AWS Secrets Manager secret format

```json
{
  "username": "pguser",
  "password": "supersecret",
  "host": "mydb.xxxx.us-east-1.rds.amazonaws.com",
  "port": "5432",
  "dbname": "awslensdb"
}
```

```bash
aws secretsmanager create-secret \
  --name prod/awslens/db \
  --region us-east-1 \
  --secret-string '{"username":"pguser","password":"CHANGEME","host":"<rds-endpoint>","port":"5432","dbname":"awslensdb"}'
```

---

## Deploying to AWS ECS

```bash
chmod +x scripts/deploy.sh
./scripts/deploy.sh 123456789012 us-east-1 v1.0.0
```

Then:
1. Attach `scripts/iam-task-policy.json` to the ECS Task Role
2. Create an ECS Task Definition pointing to the ECR image
3. Set env vars: `DB_SECRET_NAME`, `AWS_REGION`, `FLASK_SECRET_KEY`
4. Configure ALB health check: `GET /health`

---

## IAM permissions for scanning

The AWS credentials entered in the UI need read-only access.
Attach the AWS-managed `ReadOnlyAccess` policy, or scope to just:

```
ec2:Describe*
rds:Describe*
s3:ListAllMyBuckets
lambda:ListFunctions
ecs:ListClusters
eks:ListClusters
elasticache:DescribeCacheClusters
dynamodb:ListTables
cloudfront:ListDistributions
elasticloadbalancing:DescribeLoadBalancers
secretsmanager:ListSecrets
sns:ListTopics
sqs:ListQueues
redshift:DescribeClusters
sts:GetCallerIdentity
```

---

## API

| Method | Path                          | Description             |
|--------|-------------------------------|-------------------------|
| GET    | `/`                           | Scan input form         |
| POST   | `/scan`                       | Run scan & save         |
| GET    | `/result/<id>`                | Scan result page        |
| GET    | `/history`                    | History (+ date filter) |
| POST   | `/history/<id>/delete`        | Delete a session        |
| GET    | `/api/history`                | JSON history list       |
| GET    | `/health`                     | Health check            |
