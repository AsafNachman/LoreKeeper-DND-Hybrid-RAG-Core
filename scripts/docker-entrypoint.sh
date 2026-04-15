#!/bin/sh
# Baked into the image as /entrypoint.sh (outside /app) so bind-mount `.:/app`
# cannot replace this script. Local runs: `sh scripts/docker-entrypoint.sh` from /app.
set -e
cd /app

mkdir -p storage data db

# Bind-mounting a missing file can create a directory on some setups; fail fast.
if [ -d storage/chat_history.json ]; then
  echo "ERROR: storage/chat_history.json is a directory. On host: rm -rf storage/chat_history.json; echo [] > storage/chat_history.json" >&2
  exit 1
fi

# Ensure history file exists before the app runs (first boot / empty volume).
if [ ! -f storage/chat_history.json ]; then
  printf '%s\n' '[]' > storage/chat_history.json
fi

python -c 'from services.health_server import start_health_server_background; start_health_server_background()'

streamlit run app.py \
  --server.address=0.0.0.0 \
  --server.port=8501 \
  --server.headless=true &
STREAMLIT_PID=$!

trap 'kill "$STREAMLIT_PID" 2>/dev/null; exit' TERM INT

i=0
while ! curl -sf "http://127.0.0.1:8501/_stcore/health" >/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 180 ]; then
    echo "Timeout waiting for Streamlit /_stcore/health" >&2
    exit 1
  fi
  sleep 1
done

# Trigger one local page render so Streamlit executes app startup and
# process-level engine prewarm immediately (before first real user visit).
curl -sf "http://127.0.0.1:8501/?prewarm=1" >/dev/null || true

echo "Ready"

wait "$STREAMLIT_PID"

