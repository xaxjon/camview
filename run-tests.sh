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
sleep 1   # let the RTSP ports bind before the fake cameras connect

# --- fake cameras ---
./bin/ffmpeg -hide_banner -loglevel error -re \
  -f lavfi -i testsrc=size=1280x720:rate=25 -f lavfi -i sine=frequency=440 \
  -c:v libx264 -preset ultrafast -g 25 -c:a aac -rtsp_transport tcp \
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
# random port: an orphaned server from a previous run must never shadow us
TEST_PORT=$(python3 - << 'EOF'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
EOF
)
echo "test web port: $TEST_PORT"
(cd "$WORK" && PHP_CLI_SERVER_WORKERS=6 MTX_API=http://127.0.0.1:29997 MTX_RTSP_PORT=28554 php -S "127.0.0.1:$TEST_PORT" > /tmp/php.log 2>&1) & PIDS+=($!)
sleep 3
curl -sf "http://127.0.0.1:$TEST_PORT/api/me.php" > /dev/null || { echo "FAIL: php server did not start"; exit 1; }
export CAMVIEW_TEST_PORT="$TEST_PORT"

# --- motion supervisor test: moving camera records, static does not ---
mkdir -p "$MWORK"

# fake failure modes on 38554/38555:
#  - stallcam sits behind a proxy that relays the mov stream for 8s, then
#    freezes with sockets held open (half-open connection, no data, no RST)
#  - deadcam is a socket that accepts and never speaks
python3 - << 'EOF' & PIDS+=($!)
import socket, threading, time

def freeze_proxy(listen, upstream, freeze_after):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen)); srv.listen(4)
    while True:
        c, _ = srv.accept()
        try:
            u = socket.create_connection(upstream)
        except OSError:
            c.close(); continue
        freeze_at = time.time() + freeze_after
        def pump(src, dst, fa=freeze_at):
            while True:
                if time.time() > fa:
                    time.sleep(3600)  # frozen: sockets open, no data
                try:
                    d = src.recv(65536)
                except OSError:
                    return
                if not d:
                    return
                try:
                    dst.sendall(d)
                except OSError:
                    return
        threading.Thread(target=pump, args=(c, u), daemon=True).start()
        threading.Thread(target=pump, args=(u, c), daemon=True).start()

def dead_acceptor(listen):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen)); srv.listen(4)
    conns = []
    while True:
        conns.append(srv.accept()[0])  # keep the socket open, never speak

threading.Thread(target=freeze_proxy, args=(38554, ("127.0.0.1", 18554), 8), daemon=True).start()
threading.Thread(target=dead_acceptor, args=(38555,), daemon=True).start()
threading.Event().wait()
EOF

printf '[{"name":"mov","source":"rtsp://127.0.0.1:18554/mov","motion":true,"motion_threshold":0.03},{"name":"static","source":"rtsp://127.0.0.1:18554/static","motion":true,"motion_threshold":0.03},{"name":"zonecam","source":"rtsp://127.0.0.1:18554/mov","motion":true,"motion_threshold":0.03,"motion_zone":[0.0,0.0,0.5,0.5]},{"name":"stallcam","source":"rtsp://127.0.0.1:38554/mov","motion":true,"motion_threshold":0.03},{"name":"deadcam","source":"rtsp://127.0.0.1:38555/x","motion":true,"motion_threshold":0.03}]' \
  > "$MWORK/streams.json"
# motion.py reads the local restream (<name>__raw) like production — register
# those paths on the app's MediaMTX (the failure-mode proxies sit upstream)
for spec in "mov rtsp://127.0.0.1:18554/mov" "static rtsp://127.0.0.1:18554/static" \
            "zonecam rtsp://127.0.0.1:18554/mov" "stallcam rtsp://127.0.0.1:38554/mov" \
            "deadcam rtsp://127.0.0.1:38555/x"; do
  set -- $spec
  curl -sf -X POST "http://127.0.0.1:29997/v3/config/paths/add/$1__raw" \
    -H 'Content-Type: application/json' -d "{\"source\":\"$2\",\"sourceOnDemand\":true,\"rtspTransport\":\"tcp\"}" \
    || { echo "FAIL: cannot add ${1}__raw path"; exit 1; }
done
cp motion.py "$MWORK/"
ln -s "$ROOT/bin" "$MWORK/bin"
# RTSP -timeout set high here so the watchdog (not ffmpeg's own timeout) is
# what must catch the stall; the timeout option itself is verified below.
# STALL_TIMEOUT=6s: mediamtx tears down a frozen upstream after ~10s and
# reconnects, so reader-visible gaps are ~10s — the watchdog must fire
# inside one cycle, while healthy 25fps streams tick showinfo constantly.
MOTION_DIR="$MWORK/motion" MOTION_POLL_INTERVAL=2 \
  MOTION_STALL_TIMEOUT=6 MOTION_TIMEOUT_US=60000000 \
  MOTION_RTSP_BASE="rtsp://127.0.0.1:28554" \
  python3 "$MWORK/motion.py" > "$MWORK/log.txt" 2>&1 & PIDS+=($!)
sleep 25
MOV_COUNT=$(find "$MWORK/motion/mov" -name '*.jpg' 2>/dev/null | wc -l)
STATIC_COUNT=$(find "$MWORK/motion/static" -name '*.jpg' 2>/dev/null | wc -l)
echo "motion supervisor: mov=$MOV_COUNT jpegs, static=$STATIC_COUNT jpegs"
if [ "$MOV_COUNT" -lt 2 ]; then echo "FAIL: moving camera produced <2 jpegs"; exit 1; fi
if [ "$STATIC_COUNT" -gt 0 ]; then echo "FAIL: static camera produced jpegs"; exit 1; fi
[ -f "$MWORK/motion/.htaccess" ] || { echo "FAIL: motion .htaccess missing"; exit 1; }
echo "motion supervisor: ok"

# --- zone camera: crop-gated detection, full-frame capture via capturer ---
pgrep -af 'motion/zonecam' | grep -q 'crop=' \
  || { echo "FAIL: zonecam detector has no crop filter"; pgrep -af 'motion/zonecam'; exit 1; }
pgrep -af 'latest.jpg' | grep -q 'update 1' \
  || { echo "FAIL: zonecam capturer missing"; pgrep -af 'motion/zonecam'; exit 1; }
ZONE_COUNT=$(find "$MWORK/motion/zonecam" -name 'zonecam-*.jpg' 2>/dev/null | wc -l)
[ "$ZONE_COUNT" -ge 1 ] || { echo "FAIL: zonecam produced no timeline jpegs"; ls -la "$MWORK/motion/zonecam" "$MWORK/motion/zonecam/.trig" 2>/dev/null; exit 1; }
TRIG_LEFT=$(find "$MWORK/motion/zonecam/.trig" -name '*.jpg' 2>/dev/null | wc -l)
[ "$TRIG_LEFT" -le 2 ] || { echo "FAIL: trigger frames not harvested ($TRIG_LEFT left)"; exit 1; }
grep -q "started zonecam (threshold=0.03, skip_frame=True, zone=(0.0, 0.0, 0.5, 0.5)" "$MWORK/log.txt" \
  || { echo "FAIL: zonecam not started with its zone"; grep zonecam "$MWORK/log.txt"; exit 1; }
dims() { ./bin/ffmpeg -i "$1" -f null - 2>&1 | grep -m1 'Stream.*Video' | grep -o '[0-9]\{2,\}x[0-9]\{2,\}' | head -1; }
MOV_DIMS=$(dims "$(find "$MWORK/motion/mov" -name 'mov-*.jpg' | head -1)")
ZONE_DIMS=$(dims "$(find "$MWORK/motion/zonecam" -name 'zonecam-*.jpg' | head -1)")
[ -n "$MOV_DIMS" ] && [ "$MOV_DIMS" = "$ZONE_DIMS" ] \
  || { echo "FAIL: zone capture is not full frame ($ZONE_DIMS vs $MOV_DIMS)"; exit 1; }
echo "motion zone: ok"

# --- stall recovery: frozen and dead connections must be killed+restarted ---
sleep 10   # proxy freezes at ~8s; watchdog (6s) fires inside the first gap
grep -q "stallcam: no decoded frames" "$MWORK/log.txt" \
  || { echo "FAIL: watchdog did not kill the frozen stream"; cat "$MWORK/log.txt"; exit 1; }
# deadcam: its raw path never becomes ready, so ffmpeg may either be
# watchdog-killed (held session, no data) or exit and restart — both count
DEAD_RE=$(grep -c "deadcam exited" "$MWORK/log.txt" || true)
if ! grep -q "deadcam: no decoded frames" "$MWORK/log.txt" && [ "$DEAD_RE" -lt 1 ]; then
  echo "FAIL: dead camera was neither watchdog-killed nor restarted"; cat "$MWORK/log.txt"; exit 1
fi
grep -q "static: no decoded frames" "$MWORK/log.txt" \
  && { echo "FAIL: healthy idle camera wrongly flagged as stalled"; exit 1; }
grep -q "mov: no decoded frames" "$MWORK/log.txt" \
  && { echo "FAIL: healthy moving camera wrongly flagged as stalled"; exit 1; }
MOV_COUNT2=$(find "$MWORK/motion/mov" -name '*.jpg' 2>/dev/null | wc -l)
[ "$MOV_COUNT2" -gt "$MOV_COUNT" ] \
  || { echo "FAIL: healthy camera stopped producing while stalls were handled"; exit 1; }
echo "motion stall recovery: ok"

# --- zone long-run: both zone processes must stay alive and the capturer
# must keep refreshing (the ffmpeg7 overlay/two-input graphs wedged ~60s in;
# the detector+capturer pair must not) ---
L1=$(stat -c %Y "$MWORK/motion/zonecam/.latest.jpg" 2>/dev/null || echo 0)
sleep 30
L2=$(stat -c %Y "$MWORK/motion/zonecam/.latest.jpg" 2>/dev/null || echo 0)
[ "$L2" -gt "$L1" ] || { echo "FAIL: zonecam capturer stopped refreshing"; exit 1; }
NPROCS=$(pgrep -cf 'motion/zonecam')
[ "$NPROCS" -ge 2 ] || { echo "FAIL: zonecam processes missing ($NPROCS)"; pgrep -af 'motion/zonecam'; exit 1; }
echo "zone long-run: ok"

# --- RTSP -timeout: ffmpeg must error out of a silent socket on its own ---
RW_START=$(date +%s)
./bin/ffmpeg -hide_banner -loglevel error -timeout 5000000 \
  -rtsp_transport tcp -i rtsp://127.0.0.1:38555/x -f null - 2>/dev/null
RW_RC=$?
RW_ELAPSED=$(( $(date +%s) - RW_START ))
echo "rtsp -timeout probe: rc=$RW_RC after ${RW_ELAPSED}s"
if [ "$RW_RC" -eq 0 ] || [ "$RW_ELAPSED" -ge 25 ]; then
  echo "FAIL: ffmpeg hung on a dead socket despite -timeout"; exit 1
fi
echo "rtsp socket timeout: ok"

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
