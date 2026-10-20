#!/usr/bin/env bash
# Download the binaries rtsp-viewer needs into bin/. If the static ffmpeg
# mirror is blocked or returns an error page, fall back to the distro ffmpeg.
# The binaries are NOT shipped in the git repo — run this once after cloning.
set -euo pipefail
cd "$(dirname "$0")"
DEST="${1:-bin}"   # optional override, used by tests
mkdir -p "$DEST"

MEDIAMTX_VERSION="v1.20.1"

case "$(uname -m)" in
  x86_64)  ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

download() {
  local url=$1 out=$2 tmp="$2.download"
  rm -f "$tmp"
  if curl --fail --location --retry 3 --retry-delay 2 --connect-timeout 20 --output "$tmp" "$url"; then
    mv "$tmp" "$out"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

if [ -x "$DEST/mediamtx" ]; then
  echo "mediamtx already present, skipping"
else
  echo "downloading MediaMTX $MEDIAMTX_VERSION (linux/$ARCH)..."
  mediamtx_archive="$DEST/mediamtx.tar.gz"
  if ! download "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${ARCH}.tar.gz" "$mediamtx_archive"; then
    echo "MediaMTX download failed" >&2
    exit 1
  fi
  if ! tar -xzf "$mediamtx_archive" -C "$DEST" mediamtx LICENSE; then
    rm -f "$mediamtx_archive"
    echo "MediaMTX archive is not valid" >&2
    exit 1
  fi
  rm -f "$mediamtx_archive"
fi

if [ -x "$DEST/ffmpeg" ]; then
  echo "ffmpeg already present, skipping"
else
  echo "downloading static ffmpeg (linux/$ARCH)..."
  ffmpeg_archive="$DEST/ffmpeg.tar.xz"
  ffmpeg_ok=0
  if download "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${ARCH}-static.tar.xz" "$ffmpeg_archive"; then
    if tar -xJf "$ffmpeg_archive" -C "$DEST" --strip-components=1 --wildcards '*/ffmpeg'; then
      ffmpeg_ok=1
    fi
  fi
  rm -f "$ffmpeg_archive"
  if [ "$ffmpeg_ok" -ne 1 ]; then
    echo "static ffmpeg unavailable; falling back to the distro ffmpeg" >&2
    if ! command -v ffmpeg > /dev/null && [ "${EUID:-$(id -u)}" -eq 0 ] && command -v apt-get > /dev/null; then
      apt-get update -qq && apt-get install -y ffmpeg
    fi
    if command -v ffmpeg > /dev/null; then
      ln -sf "$(command -v ffmpeg)" "$DEST/ffmpeg"
    else
      echo "ffmpeg is unavailable; install it with: sudo apt install ffmpeg" >&2
      exit 1
    fi
  fi
fi

echo "done: $DEST/mediamtx and $DEST/ffmpeg ready"

# never let the web server serve the binaries directory
printf 'Require all denied\n' > "$DEST/.htaccess"
