#!/usr/bin/env bash
# Trigger a one-off seed job (no email, just records current jobs as seen).
# Run this once after the image is deployed, before the first scheduled run.
set -euo pipefail

kubectl create job job-watch-seed \
  --namespace job-watch \
  --from=cronjob/job-watch \
  --dry-run=client -o json \
| python3 -c "
import sys, json
doc = json.load(sys.stdin)
doc['spec']['template']['spec']['containers'][0]['args'] = ['python', '-m', 'job_watch', '--seed']
print(json.dumps(doc))
" | kubectl apply -f -

echo "Seed job created. Watch with: kubectl logs -n job-watch -l job-name=job-watch-seed -f"
