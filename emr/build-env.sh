#!/usr/bin/env bash
# Pack the scanner's Python deps for EMR (built on the AL2023 review box so the wheels match EMR 7's Python 3.9),
# and upload the scanner code. Run with: AWS_PROFILE=admin bash build-env.sh
set -euo pipefail
cd "$(dirname "$0")"
REGION=us-east-1
BOX=${BOX:-i-0e60c9d566555bfe5}
DEST=s3://ambitus-assets/review-runs/_emr
(cd .. && rm -f /tmp/dsscan.zip && zip -qr /tmp/dsscan.zip dsscan -x '*__pycache__*')
aws s3 cp /tmp/dsscan.zip $DEST/code/dsscan.zip --sse AES256 --only-show-errors
aws s3 cp job.py $DEST/code/job.py --sse AES256 --only-show-errors
REQS=$(tr '\n' ' ' < requirements.txt)
CMD="set -e; cd /tmp; rm -rf dsenv; python3 -m venv dsenv; ./dsenv/bin/pip install -q --upgrade pip; ./dsenv/bin/pip install -q venv-pack $REQS; ./dsenv/bin/venv-pack -p dsenv -o dsscan_env.tar.gz -f; python3 --version; aws s3 cp dsscan_env.tar.gz $DEST/env/dsscan_env.tar.gz --sse AES256 --only-show-errors; ls -la dsscan_env.tar.gz"
ID=$(aws ssm send-command --region $REGION --instance-ids $BOX --document-name AWS-RunShellScript \
  --parameters "commands=[\"$CMD\"]" --timeout-seconds 900 --query Command.CommandId --output text)
echo "building on $BOX (command $ID)..."
aws ssm wait command-executed --region $REGION --command-id "$ID" --instance-id $BOX || true
aws ssm get-command-invocation --region $REGION --command-id "$ID" --instance-id $BOX \
  --query '[Status,StandardOutputContent,StandardErrorContent]' --output text
