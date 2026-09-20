#!/usr/bin/env bash
set -e

source .env

echo "===> Deploying chart with helm-secrets (vals + Vault)..."
helm-secrets upgrade --install uptime ./uptime-monitor \
  -f ./uptime-monitor/values.yaml \
  -f ./uptime-monitor/secrets.yaml

echo "===> Deployment completed successfully!"
 