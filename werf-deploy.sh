#!/usr/bin/env bash
set -e

# 1. Подтягиваем переменные окружения и токен Vault
if [ -f .env ]; then
  source .env
else
  echo "Error: .env not found!"
  exit 1
fi

echo "===> [WERF] Checking Vault connection..."
vault_status=$(vals get "ref+vault://secret/uptime#/POSTGRES_PASSWORD" 2>/dev/null || echo "error")
if [ "$vault_status" = "error" ]; then
  echo "Error: Cannot connect to Vault!"
  exit 1
fi
echo "===> [WERF] Vault connection: OK"

echo "===> [WERF] Deploying project with werf converge..."
werf converge --dev --repo :local
echo "===> [WERF] Deployment completed successfully!"
