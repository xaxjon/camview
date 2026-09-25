#!/usr/bin/env bash
# Camview installer. Run from the repo directory after cloning:
#   git clone https://github.com/xaxjon/camview.git && cd camview && sudo ./install.sh
#
# What it does:
#   1. checks prerequisites (python3, php + php-curl, curl, tar; offers apt install)
#   2. downloads bin/mediamtx + bin/ffmpeg (setup.sh) if missing
#   3. creates data files (streams.json from example, users.json, snapshots/)
#   4. generates mediamtx.yml
#   5. sets ownership: files -> you, group -> web server (setgid), data files
#      writable by the web server so the admin UI can save
#   6. installs + enables + starts the mediamtx-viewer systemd service with the
#      correct path for THIS directory
#
# Env overrides: WEB_USER=www-data  SKIP_SYSTEMD=1  CAMVIEW_ALLOW_NONROOT=1
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

# ---------- preflight ----------

if [ "${CAMVIEW_ALLOW_NONROOT:-}" != 1 ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "run with sudo: sudo ./install.sh" >&2
  exit 1
fi
REAL_USER="${SUDO_USER:-$USER}"

WEB_USER="${WEB_USER:-}"
if [ -z "$WEB_USER" ]; then
  for u in www-data apache nginx; do
    if id "$u" > /dev/null 2>&1; then WEB_USER="$u"; break; fi
  done
fi
[ -n "$WEB_USER" ] || { echo "cannot detect web server user — set WEB_USER=..." >&2; exit 1; }

echo "== camview install in $DIR (user: $REAL_USER, web: $WEB_USER)"

# ---------- prerequisites ----------

missing=()
command -v python3 > /dev/null || missing+=(python3)
command -v curl    > /dev/null || missing+=(curl)
command -v tar     > /dev/null || missing+=(tar)
command -v php     > /dev/null || missing+=(php-cli)
if command -v php > /dev/null && ! php -m 2>/dev/null | grep -qi '^curl$'; then
  missing+=(php-curl)
fi
# the app is served by Apache + mod_php; pull them in when absent
command -v apache2ctl > /dev/null || missing+=(apache2 libapache2-mod-php)

if [ ${#missing[@]} -gt 0 ]; then
  echo "missing packages: ${missing[*]}"
  if command -v apt-get > /dev/null && [ "${CAMVIEW_ALLOW_NONROOT:-}" != 1 ]; then
    read -r -p "install them with apt now? [Y/n] " ans
    if [ "${ans:-Y}" != n ] && [ "${ans:-Y}" != N ]; then
      apt-get update -qq && apt-get install -y "${missing[@]}"
    else
      echo "install them and re-run: sudo apt install ${missing[*]}" >&2; exit 1
    fi
  else
    echo "install them and re-run: sudo apt install ${missing[*]}" >&2; exit 1
  fi
fi

# ---------- binaries ----------

if [ ! -x bin/mediamtx ] || [ ! -x bin/ffmpeg ]; then
  echo "== downloading binaries"
  ./setup.sh
fi

# ---------- data files ----------

[ -f streams.json ] || cp streams.json.example streams.json
[ -f users.json ] || touch users.json
mkdir -p snapshots

echo "== generating mediamtx.yml"
python3 gen-config.py

# ---------- ownership ----------

# App files owned by the installing user (so git pull works); group is the
# web server with setgid dirs so PHP can atomically write the data files.
echo "== setting ownership ($REAL_USER:$WEB_USER)"
chown -R "$REAL_USER:$WEB_USER" .
find . -type d -not -path './.git/*' -exec chmod 2775 {} +
chmod 0660 streams.json mediamtx.yml users.json

URL_PATH="${DIR#/var/www/html}"
URL_PATH="${URL_PATH:-/}"

# ---------- systemd service ----------

if [ "${SKIP_SYSTEMD:-}" = 1 ]; then
  echo "== SKIP_SYSTEMD=1, not installing service"
else
  echo "== installing mediamtx-viewer.service"
  sed "s|__DIR__|$DIR|g" mediamtx-viewer.service > /etc/systemd/system/mediamtx-viewer.service
  echo "== installing camview-motion.service"
  sed "s|__DIR__|$DIR|g" camview-motion.service > /etc/systemd/system/camview-motion.service
  systemctl daemon-reload
  systemctl enable --now mediamtx-viewer
  systemctl is-active --quiet mediamtx-viewer \
    && echo "   mediamtx-viewer active" \
    || { echo "   mediamtx-viewer FAILED to start — check: journalctl -u mediamtx-viewer -n 30" >&2; exit 1; }
  systemctl enable --now camview-motion
  systemctl is-active --quiet camview-motion \
    && echo "   camview-motion active" \
    || { echo "   camview-motion FAILED to start — check: journalctl -u camview-motion -n 30" >&2; exit 1; }
fi

# ---------- apache ----------

if [ "${SKIP_APACHE:-}" = 1 ]; then
  echo "== SKIP_APACHE=1, not configuring Apache"
elif command -v apache2ctl > /dev/null 2>&1; then
  echo "== configuring Apache"
  # the app needs mod_php, not just php-cli
  if ! apache2ctl -M 2>/dev/null | grep -qi 'php'; then
    echo "   installing libapache2-mod-php"
    apt-get install -y libapache2-mod-php || true
  fi
  apache2ctl -M 2>/dev/null | grep -qi 'php' \
    || { echo "   ERROR: Apache PHP module still missing — the app will not work" >&2; exit 1; }
  # protection that does NOT depend on AllowOverride (the stock .htaccess
  # is silently ignored when the vhost has AllowOverride None)
  sed "s|__DIR__|$DIR|g" apache.conf > /etc/apache2/conf-available/camview.conf
  a2enconf camview > /dev/null
  systemctl reload apache2
  echo "   camview.conf enabled (data files + motion/ + snapshots/ + .git denied)"
  # prove it
  if curl -sf -o /dev/null "http://127.0.0.1${URL_PATH%/}/streams.json"; then
    echo "   WARNING: streams.json is STILL downloadable — check the vhost config" >&2
  else
    echo "   verified: streams.json is not downloadable"
  fi
else
  echo "== Apache not detected"
  echo "   serve $DIR with any web server + PHP, but you MUST deny direct"
  echo "   download of: *.json, mediamtx.yml, motion/, snapshots/, .git"
fi

# ---------- done ----------

echo
echo "Done. Next steps:"
echo "  1. open http://<this-host>${URL_PATH%/}/setup.html  (create the admin)"
echo "  2. log in, open Cameras, add/edit cameras (or edit streams.json + sudo python3 gen-config.py && sudo systemctl restart mediamtx-viewer)"
echo "  3. verify credentials are protected:"
echo "     curl -sI http://127.0.0.1${URL_PATH%/}/streams.json | head -1   # expect 403"
