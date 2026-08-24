#!/usr/bin/env bash
# Start MediaMTX and the viewer web server. Ctrl-C stops both.
set -e
cd "$(dirname "$0")"

python3 gen-config.py

./bin/mediamtx mediamtx.yml &
MTX_PID=$!

python3 -m http.server "${PORT:-8080}" &
WEB_PID=$!

trap 'kill $MTX_PID $WEB_PID 2>/dev/null || true' EXIT
echo "Viewer:  http://$(hostname -I | awk '{print $1}'):${PORT:-8080}/"
wait
