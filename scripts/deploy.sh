#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Build & push the AWS Lens image to Amazon ECR
#
# Usage:
#   ./scripts/deploy.sh [AWS_ACCOUNT_ID] [AWS_REGION] [TAG]
#
# Prerequisites: AWS CLI v2, Docker, appropriate IAM permissions
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

AWS_ACCOUNT_ID="${1:-$(aws sts get-caller-identity --query Account --output text)}"
AWS_REGION="${2:-us-east-1}"
TAG="${3:-latest}"
REPO="awslens"

REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
FULL="${REGISTRY}/${REPO}:${TAG}"

echo "▶  Authenticating with ECR…"
aws ecr get-login-password --region "${AWS_REGION}" \
  | docker login --username AWS --password-stdin "${REGISTRY}"

echo "▶  Creating ECR repo (idempotent)…"
aws ecr describe-repositories --repository-names "${REPO}" --region "${AWS_REGION}" \
  > /dev/null 2>&1 \
  || aws ecr create-repository \
      --repository-name "${REPO}" \
      --region "${AWS_REGION}" \
      --image-scanning-configuration scanOnPush=true \
      --encryption-configuration encryptionType=AES256 \
      > /dev/null

echo "▶  Building for linux/amd64…"
docker build --platform linux/amd64 -t "${FULL}" -t "${REGISTRY}/${REPO}:latest" .

echo "▶  Pushing…"
docker push "${FULL}"
docker push "${REGISTRY}/${REPO}:latest"

echo ""
echo "✅  Image pushed: ${FULL}"
echo ""
echo "─── Next steps ───────────────────────────────────────────────────────────"
echo "1. Create Secrets Manager secret:"
echo "   aws secretsmanager create-secret \\"
echo "     --name prod/awslens/db \\"
echo "     --region ${AWS_REGION} \\"
echo "     --secret-string '{\"username\":\"pguser\",\"password\":\"CHANGEME\",\"host\":\"<rds-endpoint>\",\"port\":\"5432\",\"dbname\":\"awslensdb\"}'"
echo ""
echo "2. Grant ECS Task Role access (see scripts/iam-task-policy.json)."
echo ""
echo "3. Create ECS Task Definition with:"
echo "   Image URI : ${FULL}"
echo "   Port      : 5000"
echo "   Env vars  : DB_SECRET_NAME=prod/awslens/db"
echo "               AWS_REGION=${AWS_REGION}"
echo "               FLASK_SECRET_KEY=<random>"
echo "   Health    : GET /health → 200"
