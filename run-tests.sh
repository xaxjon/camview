#!/usr/bin/env bash
# Run the camview test suites (API-level + UI-level) against a scratch
# environment: two throwaway MediaMTX instances on alternate ports, fake
# cameras (AAC testsrc, moving mandelbrot, static color), and the app served
# by PHP's built-in web server.
#
# Usage: ./run-tests.sh     (requires: python3 + websockets, google-chrome,
#                          php, and bin/ binaries via ./setup.sh)
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
WORK=/tmp/camview-test
MWORK=/tmp/camview-motion-test
PIDS=()

cleanup() { kill "${PIDS[@]}" 2>/dev/null; rm -rf "$WORK" "$MWORK"; }
trap cleanup EXIT

# --- scratch app copy (binaries symlinked) ---
rm -rf "$WORK" "$MWORK"
mkdir -p "$WORK"
cp -r api *.html gen-config.py transcode.py motion.py streams.json.example "$WORK/"
ln -s "$ROOT/bin" "$WORK/bin"
cp streams.json.example "$WORK/streams.json"

# --- scratch MediaMTX instances ---
cat > /tmp/mtx-test-camera.yml << 'EOF'   # hosts the fake cameras (publisher mode)
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
  mov:
  static:
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

# --- fake cameras ---
./bin/ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i testsrc=size=1280x720:rate=25 -f lavfi -i sine=frequency=440 \
  -c:v libx264 -preset ultrafast -c:a aac -rtsp_transport tcp \
  -f rtsp rtsp://127.0.0.1:18554/test > /tmp/ff_cam.log 2>&1 & PIDS+=($!)
# moving picture (scene scores >> threshold): mandelbrot zoom
./bin/ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i mandelbrot=size=640x480:rate=25 \
  -c:v libx264 -preset ultrafast -g 25 -rtsp_transport tcp \
  -f rtsp rtsp://127.0.0.1:18554/mov > /tmp/ff_mov.log 2>&1 & PIDS+=($!)
# static picture (scene score ~ 0)
./bin/ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i color=c=red:size=640x480:rate=25 \
  -c:v libx264 -preset ultrafast -g 25 -rtsp_transport tcp \
  -f rtsp rtsp://127.0.0.1:18554/static > /tmp/ff_static.log 2>&1 & PIDS+=($!)

# --- app under PHP's built-in server, pointed at the scratch MediaMTX ---
(cd "$WORK" && MTX_API=http://127.0.0.1:29997 php -S 127.0.0.1:8099 > /tmp/php.log 2>&1) & PIDS+=($!)
sleep 3

# --- motion supervisor test: moving camera records, static does not ---
mkdir -p "$MWORK"
printf '[{"name":"mov","source":"rtsp://127.0.0.1:18554/mov","motion":true,"motion_threshold":0.03},{"name":"static","source":"rtsp://127.0.0.1:18554/static","motion":true,"motion_threshold":0.03}]' \
  > "$MWORK/streams.json"
cp motion.py "$MWORK/"
ln -s "$ROOT/bin" "$MWORK/bin"
(cd "$MWORK" && MOTION_DIR="$MWORK/motion" MOTION_POLL_INTERVAL=2 nohup python3 motion.py > "$MWORK/log.txt" 2>&1) & PIDS+=($!)
sleep 25
MOV_COUNT=$(find "$MWORK/motion/mov" -name '*.jpg' 2>/dev/null | wc -l)
STATIC_COUNT=$(find "$MWORK/motion/static" -name '*.jpg' 2>/dev/null | wc -l)
echo "motion supervisor: mov=$MOV_COUNT jpegs, static=$STATIC_COUNT jpegs"
if [ "$MOV_COUNT" -lt 2 ]; then echo "FAIL: moving camera produced <2 jpegs"; exit 1; fi
if [ "$STATIC_COUNT" -gt 0 ]; then echo "FAIL: static camera produced jpegs"; exit 1; fi
[ -f "$MWORK/motion/.htaccess" ] || { echo "FAIL: motion .htaccess missing"; exit 1; }
echo "motion supervisor: ok"

reset_state() {
  rm -rf "$WORK/snapshots" "$WORK/motion"
  rm -f "$WORK/users.json"
  cp streams.json.example "$WORK/streams.json"
  # seed motion files for testcam so API/UI tests have timeline data
  local day dir
  day=$(date +%Y-%m-%d)
  dir="$WORK/motion/testcam/$day"
  mkdir -p "$dir"
  for i in 0 1 2; do
    ts=$(date -d "now - $((i * 60)) sec" +%Y%m%d-%H%M%S)
    ./bin/ffmpeg -hide_banner -loglevel error -f lavfi -i "color=c=blue:size=320x240" \
      -frames:v 1 -y "$dir/testcam-$ts.jpg"
  done
}

reset_state
python3 test_api.py || exit 1
reset_state
python3 test_ui.py || exit 1
