# Camview

A multi-camera RTSP grid viewer for the browser. MediaMTX pulls the RTSP
streams and re-serves them over WebRTC (sub-second latency); a vanilla-JS
frontend renders an auto-sizing grid, and a small PHP API provides login,
user management and a camera config UI. No build step, no framework.

## Features

- Grid picks the column count that maximizes tile size (16:9 tiles) so all
  cameras fit the viewport without scrolling. Recomputed on resize.
- **Single click** a tile → fullscreen. **Double click** → back to grid.
- **Login with roles**: `admin` (manage cameras + users) and `viewer`
  (watch only). First-run `setup.html` creates the initial admin.
- **Camera config UI** (`admin.html`): add/edit/delete cameras with a
  **live connection test** (probes the RTSP URL, reports video/audio
  codecs, and warns when audio needs transcoding). Changes apply to the
  running MediaMTX instantly — no service restart.
- **User management** (`users.html`): create users, set roles, reset
  passwords; protects the last admin.
- **Live status dots** per camera (streaming / standby / disabled).
- **Enable/disable** a camera without losing its config.
- **Snapshots**: the 📷 button on a tile saves a JPEG on the server
  (`snapshots/<cam>-<date>-<time>.jpg`). The **Snapshots page**
  (`snapshots.html`) offers thumbnails, full view, single or bulk download
  (`.tar.gz`), bulk delete, and a full purge. Oldest files are auto-pruned
  beyond 500 snapshots. Delete/purge are admin-only; any user can take,
  view and download snapshots.
- Per-tile audio toggle (streams start muted so autoplay works).
- Streams are pulled **on demand**: cameras are only connected while at
  least one viewer is watching; everything stops ~10s after the last
  viewer leaves.

## Requirements

- Linux x86_64 or aarch64
- **PHP 8+** with curl (for the login/config backend) and a web server
  (Apache, or `php -S` for a quick look)
- **python3** (runs `gen-config.py` to regenerate the MediaMTX config)
- MediaMTX + ffmpeg binaries: not in the repo — run `./setup.sh` once

## Setup

```sh
./setup.sh                          # downloads bin/mediamtx + bin/ffmpeg
cp streams.json.example streams.json
```

Then either run manually:

```sh
./start.sh                          # MediaMTX + viewer on :8080 (no PHP features)
```

…or, for the full experience (login, config UI), serve the directory with
Apache + PHP and run MediaMTX as a service:

```sh
sudo cp mediamtx-viewer.service /etc/systemd/system/
# adjust ExecStart paths in the unit to your install directory
sudo systemctl daemon-reload && sudo systemctl enable --now mediamtx-viewer
```

Deployment permissions: the web server user must be able to write
`streams.json`, `mediamtx.yml`, `users.json` and the `snapshots/` directory
in the install dir:

```sh
sudo chown www-data streams.json mediamtx.yml   # users.json and snapshots/ are created by the app
```

Open `http://<host>/…/setup.html` once to create the first admin, then log
in. The viewer grid is `index.html`; admins also get **Cameras** and
**Users** pages.

## Sound

WebRTC audio only supports **Opus** and **G.711 (PCMU/PCMA)**. The
config UI's *Test connection* button tells you what the camera sends:

- `pcm_mulaw`/`pcm_alaw` — audio works as-is.
- `aac` — MediaMTX cannot serve it over WebRTC. Either switch the camera
  to G.711 in its own web interface, or tick **Transcode audio** on the
  camera (an on-demand ffmpeg copies video, transcodes audio to Opus).

Tiles start muted (browser autoplay rules) — click the 🔊 button.

## Updating a deployment

Deployments are plain git clones of this repo — camera lists, users and
generated config are git-ignored and survive pulls:

```sh
cd /path/to/camview
git pull
# first time only, or when bin/ is missing: ./setup.sh
```

If the pull changed `gen-config.py`, refresh the running config:
`sudo python3 gen-config.py && sudo systemctl restart mediamtx-viewer`
(or just re-save a camera in the admin UI, which live-applies).
Data files stay writable by the web server across pulls because git never
touches them.

## Security notes

- `streams.json` / `users.json` / `mediamtx.yml` contain credentials and
  are git-ignored **and** denied by the shipped `.htaccess` (needs
  `AllowOverride` on Apache). Camera source URLs are never sent to
  non-admin browsers; snapshots and the grid use server-side lookups.
- The MediaMTX WebRTC port (TCP 8889 + UDP 8189) is unauthenticated by
  design — firewall it to trusted networks.
- Use HTTPS in front of Apache if logins cross untrusted links.

## Headless / no-PHP mode

`streams.json` remains the canonical camera list. Without PHP you can still
edit it by hand and run `sudo python3 gen-config.py && sudo systemctl
restart mediamtx-viewer` — the UI simply isn't available. Plain string
entries in the array act as comments.

## How it works

- `gen-config.py` turns `streams.json` into `mediamtx.yml`
  (`sourceOnDemand` pull paths, or `runOnDemand` ffmpeg transcode paths).
- The PHP API (`api/`) handles auth (PHP sessions + bcrypt in
  `users.json`), camera CRUD, and live-applies path changes through the
  MediaMTX control API — no restarts needed for camera edits.
- The viewer plays each camera via WHEP
  (`http://<host>:8889/<name>/whep`) with a recvonly `RTCPeerConnection`.
- Browser needs TCP 80/443 (or 8080), TCP 8889 (WHEP) and UDP 8189
  (WebRTC media) to the host.

## Testing

```sh
./run-tests.sh
```

spins up a scratch environment (two MediaMTX instances on alternate ports,
a fake AAC test camera, the app under `php -S`) and runs:

- `test_api.py` — 30 API checks: setup, login, CSRF, validation, CRUD with
  live MediaMTX apply, probe/snapshot, roles.
- `test_ui.py` — 13 headless-Chrome checks: first-run flow, login, grid,
  admin add-camera with live test, disable toggle, logout gating.

Requires `google-chrome` and the Python `websockets` package.

## Files

- `index.html` — viewer grid (login required)
- `login.html`, `setup.html` — auth and first-run admin creation
- `admin.html`, `users.html` — camera and user management (admin only)
- `snapshots.html` — snapshot file management (view/download all users,
  delete/purge admin only)
- `api/` — PHP backend (auth, CRUD, live test, snapshots, MediaMTX client)
- `snapshots/` — saved JPEGs, auto-created and git-ignored (direct web
  access denied; files are served through the authenticated API)
- `streams.json.example` — camera list template; copy to `streams.json`
  (git-ignored, contains credentials)
- `gen-config.py` — generates `mediamtx.yml` from `streams.json`
- `setup.sh` — downloads the MediaMTX + ffmpeg binaries into `bin/`
- `start.sh` — manual run (MediaMTX + static viewer, no PHP)
- `mediamtx-viewer.service` — systemd unit for permanent operation
- `run-tests.sh`, `test_api.py`, `test_ui.py` — test suite
