#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/lrocha/projetos/campinas-nfse-automator"
APP_URL="http://127.0.0.1:8001"
ENV_FILE="$HOME/.config/campinas-nfse-automator/env"
LOG_FILE="$APP_DIR/app.log"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

cd "$APP_DIR"

if ! curl -fsS "$APP_URL" >/dev/null 2>&1; then
  nohup python3 -m uvicorn main:app --host 127.0.0.1 --port 8001 >> "$LOG_FILE" 2>&1 &
  sleep 2
fi

xdg-open "$APP_URL" >/dev/null 2>&1 &
