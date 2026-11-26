#!/usr/bin/env bash
# Deploy the public `ask-trinity` Cloud Function (the Ask Trinity docs
# assistant: Vertex AI Search engine `trinity-search` + an answer prompt).
# Flow doc: docs/memory/feature-flows/trinity-docs-qa.md
#
# The flags reproduce the live function's configuration (revision
# ask-trinity-00006-fun) so a redeploy changes only the code. Needs a gcloud
# login with Cloud Functions deploy rights on the project.
#
#   ./src/ask-trinity/deploy.sh
#   python3 scripts/ask-trinity-link-eval.py     # then check the answers
set -euo pipefail

cd "$(dirname "$0")"

gcloud functions deploy ask-trinity \
  --project=mcp-server-project-455215 \
  --region=us-central1 \
  --gen2 \
  --runtime=python312 \
  --entry-point=ask_trinity \
  --source=. \
  --trigger-http \
  --allow-unauthenticated \
  --memory=512Mi \
  --cpu=1 \
  --timeout=120s \
  --min-instances=1 \
  --max-instances=100 \
  --concurrency=1
