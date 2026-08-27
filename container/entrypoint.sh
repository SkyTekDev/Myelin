#!/bin/sh
set -eu

PORT="${PORT:-8080}"

if [ ! -d "${MYELIN_JAR_PATH:-/app/jars}" ]; then
    echo "CMS JAR directory was not found: ${MYELIN_JAR_PATH:-/app/jars}" >&2
    exit 1
fi

if [ ! -f "${MYELIN_DB_PATH:-/app/data/myelin.db}" ]; then
    echo "Myelin database was not found: ${MYELIN_DB_PATH:-/app/data/myelin.db}" >&2
    exit 1
fi

exec python -m uvicorn bridge.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips="*" \
    --timeout-keep-alive 65
