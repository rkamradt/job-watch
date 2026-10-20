#!/usr/bin/env bash
# Copy ghcr-secret from the portfolio namespace into job-watch.
set -euo pipefail

kubectl get secret ghcr-secret -n portfolio -o json | python3 -c "
import sys, json
s = json.load(sys.stdin)
del s['metadata']['creationTimestamp']
del s['metadata']['resourceVersion']
del s['metadata']['uid']
s['metadata']['namespace'] = 'job-watch'
print(json.dumps(s))
" | kubectl apply -f -

echo "ghcr-secret copied to job-watch namespace."
