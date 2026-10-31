#!/usr/bin/env bash
# One-time EMR Serverless setup for the dataset scanner. Run with: AWS_PROFILE=admin bash emr-setup.sh
# Creates: IAM role ambitus-emr-scan-role (read collections/, read+write review-runs/), and an EMR Serverless Spark
# application capped at 64 vCPU / 256 GB that stops itself after 10 idle minutes (no cost while stopped).
set -euo pipefail
cd "$(dirname "$0")"
REGION=us-east-1
ACCOUNT=799168734775

cat > /tmp/emr-trust.json <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"emr-serverless.amazonaws.com"},
 "Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"}}}]}
JSON
cat > /tmp/emr-policy.json <<'JSON'
{"Version":"2012-10-17","Statement":[
 {"Sid":"List","Effect":"Allow","Action":"s3:ListBucket","Resource":"arn:aws:s3:::ambitus-assets",
  "Condition":{"StringLike":{"s3:prefix":["collections/","collections/*","review-runs/","review-runs/*"]}}},
 {"Sid":"ReadCollections","Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::ambitus-assets/collections/*"},
 {"Sid":"ReadWriteRuns","Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":"arn:aws:s3:::ambitus-assets/review-runs/*"}
]}
JSON
echo "== IAM role"
aws iam create-role --role-name ambitus-emr-scan-role --assume-role-policy-document file:///tmp/emr-trust.json \
  --description "EMR Serverless dataset scanner: read collections/, write review-runs/" --tags Key=Owner,Value=ajay >/dev/null
aws iam put-role-policy --role-name ambitus-emr-scan-role --policy-name DatasetScan --policy-document file:///tmp/emr-policy.json

echo "== EMR Serverless application"
APP=$(aws emr-serverless create-application --region $REGION --name ambitus-dataset-scan --type SPARK \
  --release-label emr-7.14.0 --architecture X86_64 \
  --maximum-capacity cpu=64vCPU,memory=256GB,disk=2000GB \
  --auto-start-configuration enabled=true --auto-stop-configuration enabled=true,idleTimeoutMinutes=10 \
  --tags Owner=ajay --query applicationId --output text)
echo "APP_ID=$APP" > emr.env
echo "ROLE_ARN=arn:aws:iam::$ACCOUNT:role/ambitus-emr-scan-role" >> emr.env
cat emr.env
echo "done. Next: bash build-env.sh (packs the Python deps on the review box), then bash run.sh <s3 prefix>"
