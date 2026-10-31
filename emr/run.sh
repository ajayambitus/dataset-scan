#!/usr/bin/env bash
# Start one scan: bash run.sh s3://ambitus-assets/collections/<env>/<collection>/ [name]
# Output: s3://ambitus-assets/review-runs/_emr/scans/<run>/{rows.jsonl,report.json,report.md}
set -euo pipefail
cd "$(dirname "$0")"
source emr.env
REGION=us-east-1
IN=$1; NAME=${2:-scan}
RUN=$(date -u +%Y%m%dT%H%M%SZ)-$NAME
OUT=s3://ambitus-assets/review-runs/_emr/scans/$RUN/
E=s3://ambitus-assets/review-runs/_emr
PARAMS="--py-files $E/code/dsscan.zip --conf spark.archives=$E/env/dsscan_env.tar.gz#environment \
--conf spark.emr-serverless.driverEnv.PYSPARK_DRIVER_PYTHON=./environment/bin/python \
--conf spark.emr-serverless.driverEnv.PYSPARK_PYTHON=./environment/bin/python \
--conf spark.executorEnv.PYSPARK_PYTHON=./environment/bin/python \
--conf spark.executor.cores=4 --conf spark.executor.memory=12g --conf spark.emr-serverless.executor.disk=100g \
--conf spark.driver.cores=2 --conf spark.driver.memory=8g --conf spark.dynamicAllocation.maxExecutors=15"
JOB=$(aws emr-serverless start-job-run --region $REGION --application-id "$APP_ID" --execution-role-arn "$ROLE_ARN" \
  --name "dsscan-$RUN" --execution-timeout-minutes 240 \
  --job-driver "{\"sparkSubmit\":{\"entryPoint\":\"$E/code/job.py\",\"entryPointArguments\":[\"--input\",\"$IN\",\"--output\",\"$OUT\",\"--name\",\"$NAME\"],\"sparkSubmitParameters\":\"$PARAMS\"}}" \
  --query jobRunId --output text)
echo "job $JOB -> $OUT"
echo "watch: aws emr-serverless get-job-run --region $REGION --application-id $APP_ID --job-run-id $JOB --query 'jobRun.[state,stateDetails,totalExecutionDurationSeconds]'"
