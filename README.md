# RTSP Viewer

A simple multi-camera RTSP grid viewer for the browser. MediaMTX pulls the
RTSP streams and re-serves them over WebRTC (sub-second latency); a single
vanilla-JS page renders them in an auto-sizing grid.

- Grid picks the column count that maximizes tile size (16:9 tiles) so all
  streams fit the viewport without scrolling. Recomputed on resize.
- **Single click** a tile → fullscreen. **Double click** → back to grid
  (Esc also exits fullscreen).
- Per-tile audio toggle (streams start muted so autoplay works).
- Offline streams show a status overlay and retry every 3 seconds.
- Streams are pulled **on demand**: cameras are only connected while at
  least one viewer is watching, and disconnected ~10s after the last
  viewer leaves.

## Setup

**Prerequisite:** the MediaMTX and ffmpeg binaries are **not** in the git
repo. Fetch them once after cloning (into `bin/`, Linux x86_64/aarch64):

```sh
./setup.sh
```

1. Create your camera list from the example and edit it (`streams.json`
   contains credentials and is git-ignored — never commit it):

   ```sh
   cp streams.json.example streams.json
   ```

   ```json
   [
     "string entries like this one are comments — both the viewer and gen-config.py skip them",
     { "name": "front-door", "source": "rtsp://user:pass@192.168.1.10:554/stream1" },
     { "name": "driveway",   "source": "rtsp://user:pass@192.168.1.11:554/stream1" }
   ]
   ```

   Names become MediaMTX path names: letters, digits, `-`, `_` only.
   (JSON has no real comment syntax; plain string entries are the
   convention here — keep them valid JSON strings.)

2. Run:

   ```sh
   ./start.sh          # serves the viewer on port 8080 (override with PORT=xxxx)
   ```

3. Open `http://<host>:8080/` in a browser.

`start.sh` regenerates `mediamtx.yml` from `streams.json` (via
`gen-config.py`), starts MediaMTX, and starts a static web server for the
viewer page. Ctrl-C stops both. If the directory is served by a web server
already (e.g. Apache under `/var/www/html`), only the MediaMTX half is
needed — the viewer page is purely static.

## Running permanently (recommended)

Don't tie MediaMTX's lifetime to page loads — a browser cannot reliably
signal "tab closed" (crashes, network drops), so page-driven start/stop is
fragile. Instead, run MediaMTX as a service and let its on-demand features
do the work: an idle MediaMTX uses ~10 MB of RAM and pulls **no** camera
traffic until someone opens the viewer; streams (and any transcode
processes) stop automatically seconds after the last viewer leaves.

```sh
sudo cp mediamtx-viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mediamtx-viewer
```

After editing `streams.json`: `sudo python3 gen-config.py && sudo systemctl
restart mediamtx-viewer`.

## Sound

WebRTC audio only supports **Opus** and **G.711 (PCMU/PCMA)**. If a tile is
silent even after clicking its 🔊 button:

- Check the camera's audio codec: `bin/ffmpeg -i rtsp://... -t 1 -f null -`
  and look at the `Audio:` line.
- If it says `pcm_mulaw`/`pcm_alaw` — audio should already work.
- If it says `aac`, MediaMTX **skips** that track for WebRTC (you'll see
  `skipping track (MPEG-4 Audio)` in its log). Two fixes:
  1. Preferred: switch the camera's audio codec to G.711 in its own web
     interface (most IP cameras offer this).
  2. Or set `"transcode_audio": true` on the stream in `streams.json`:

     ```json
     { "name": "cam2", "source": "rtsp://...", "transcode_audio": true }
     ```

     MediaMTX then starts an ffmpeg process on demand that copies the video
     untouched and transcodes the audio to Opus. One ffmpeg per actively
     watched stream; stopped when nobody watches.

## How it works

- `gen-config.py` turns `streams.json` into a `mediamtx.yml`. Plain streams
  get `source: <rtsp url>` + `sourceOnDemand` (pull only while watched);
  `transcode_audio` streams get a `runOnDemand` ffmpeg publisher instead.
- The viewer page fetches `streams.json`, builds one tile per stream, and
  plays each via WHEP (`http://<host>:8889/<name>/whep`) with a recvonly
  `RTCPeerConnection`.
- Firewall: the browser needs TCP 8080 (page, or 80/443 via Apache),
  TCP 8889 (WHEP handshake), and UDP 8189 (WebRTC media) to the host.

## Testing

`test_e2e.py` drives headless Chrome over CDP and verifies real WebRTC
playback plus the fullscreen toggles. It needs a live `test` stream:

```sh
./bin/mediamtx test-mediamtx.yml &                       # publishable 'test' path
./bin/ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=25 \
  -f lavfi -i sine=frequency=440 \
  -c:v libx264 -preset ultrafast -c:a aac \
  -f rtsp rtsp://127.0.0.1:8554/test &
python3 test_e2e.py                                      # prints RESULT: PASS
```

Requires `google-chrome` and the Python `websockets` package.

## Files

- `streams.json.example` — template for your camera list; copy to
  `streams.json` (git-ignored, the only file you edit)
- `index.html` — the viewer (grid layout, WHEP playback, fullscreen)
- `gen-config.py` — generates `mediamtx.yml` from `streams.json`
- `setup.sh` — downloads the MediaMTX + ffmpeg binaries into `bin/`
- `start.sh` — runs MediaMTX + web server (manual use)
- `mediamtx-viewer.service` — systemd unit for permanent operation
- `test_e2e.py`, `test-mediamtx.yml` — end-to-end test
- `bin/` — created by `setup.sh` (ffmpeg is needed for `transcode_audio`
  and the tests)
