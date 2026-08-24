#!/usr/bin/env bash
# Download the binaries rtsp-viewer needs into bin/.
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

if [ -x "$DEST/mediamtx" ]; then
  echo "mediamtx already present, skipping"
else
  echo "downloading MediaMTX $MEDIAMTX_VERSION (linux/$ARCH)..."
  curl -sL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${ARCH}.tar.gz" \
    | tar xz -C "$DEST" mediamtx LICENSE
fi

if [ -x "$DEST/ffmpeg" ]; then
  echo "ffmpeg already present, skipping"
else
  echo "downloading static ffmpeg (linux/$ARCH)..."
  curl -sL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${ARCH}-static.tar.xz" \
    | tar xJ -C "$DEST" --strip-components=1 --wildcards '*/ffmpeg'
fi

echo "done: $DEST/mediamtx and $DEST/ffmpeg ready"
