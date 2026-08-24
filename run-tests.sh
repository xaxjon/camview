#!/usr/bin/env bash
# Run the camview test suites (API-level + UI-level) against a scratch
# environment: two throwaway MediaMTX instances on alternate ports, a fake
# AAC test camera, and the app served by PHP's built-in web server.
#
# Usage: ./run-tests.sh     (requires: python3 + websockets, google-chrome,
#                          php, and bin/ binaries via ./setup.sh)
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
WORK=/tmp/camview-test
PIDS=()

cleanup() { kill "${PIDS[@]}" 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT

# --- scratch app copy (binaries symlinked) ---
rm -rf "$WORK"
mkdir -p "$WORK"
cp -r api *.html gen-config.py streams.json.example "$WORK/"
ln -s "$ROOT/bin" "$WORK/bin"
cp streams.json.example "$WORK/streams.json"

# --- scratch MediaMTX instances ---
cat > /tmp/mtx-test-camera.yml << 'EOF'   # hosts the fake camera (publisher mode)
logLevel: warn
rtsp: true
rtspAddress: :18554
rtspTransports: [tcp]
rtmp: false
srt: false
moq: false
webrtc: false
hls: false
api: true
apiAddress: 127.0.0.1:19997
paths:
  test:
EOF
cat > /tmp/mtx-test-main.yml << 'EOF'    # the instance the app manages
logLevel: warn
rtsp: true
rtspAddress: :28554
rtmp: false
srt: false
moq: false
webrtc: true
webrtcAddress: :28889
webrtcLocalUDPAddress: :28189
rtpAddress: :28000
rtcpAddress: :28001
hls: false
api: true
apiAddress: 127.0.0.1:29997
paths: {}
EOF
./bin/mediamtx /tmp/mtx-test-camera.yml > /tmp/mtxA.log 2>&1 & PIDS+=($!)
./bin/mediamtx /tmp/mtx-test-main.yml > /tmp/mtxB.log 2>&1 & PIDS+=($!)

# --- fake camera: 720p testsrc + AAC sine tone ---
./bin/ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i testsrc=size=1280x720:rate=25 -f lavfi -i sine=frequency=440 \
  -c:v libx264 -preset ultrafast -c:a aac -rtsp_transport tcp \
  -f rtsp rtsp://127.0.0.1:18554/test > /tmp/ff_cam.log 2>&1 & PIDS+=($!)

# --- app under PHP's built-in server, pointed at the scratch MediaMTX ---
(cd "$WORK" && MTX_API=http://127.0.0.1:29997 php -S 127.0.0.1:8090 > /tmp/php.log 2>&1) & PIDS+=($!)
sleep 3

reset_state() { rm -f "$WORK/users.json"; cp streams.json.example "$WORK/streams.json"; }

reset_state
python3 test_api.py || exit 1
reset_state
python3 test_ui.py || exit 1
