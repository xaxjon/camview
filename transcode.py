#!/usr/bin/env python3
"""RunOnDemand wrapper: transcode a camera's audio to Opus and republish.

Usage: transcode.py <rtsp-source> <rtsp-publish-url>

Wraps ffmpeg with a stall watchdog. ffmpeg blocked in a network read
ignores MediaMTX's SIGINT and leaks forever (frozen camera, WiFi drop).
This wrapper tracks ffmpeg's -progress output; if ffmpeg produces nothing
for STALL_TIMEOUT seconds it is killed, and MediaMTX (runOnDemandRestart)
or the next viewer starts a fresh one.
"""
import os
import select
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(ROOT, "bin", "ffmpeg")
STALL_TIMEOUT = 20  # seconds without any progress output

proc = None


def die(*_):
    if proc and proc.poll() is None:
        proc.kill()
    sys.exit(1)


def main():
    global proc
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <rtsp-source> <rtsp-publish-url>")
    source, out = sys.argv[1], sys.argv[2]

    signal.signal(signal.SIGINT, die)
    signal.signal(signal.SIGTERM, die)

    proc = subprocess.Popen(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats",
            "-rtsp_transport", "tcp", "-i", source,
            "-c:v", "copy", "-c:a", "libopus", "-ar", "48000", "-ac", "2", "-b:a", "64k",
            "-progress", "pipe:1", "-stats_period", "2",
            "-rtsp_transport", "tcp", "-f", "rtsp", out,
        ],
        stdout=subprocess.PIPE,
        # stderr stays inherited -> ends up in the MediaMTX log
    )

    last = time.monotonic()
    while proc.poll() is None:
        ready, _, _ = select.select([proc.stdout], [], [], 2)
        if ready:
            if proc.stdout.read1(65536):
                last = time.monotonic()
        elif time.monotonic() - last > STALL_TIMEOUT:
            print(f"transcode: no progress for {STALL_TIMEOUT}s, killing ffmpeg", file=sys.stderr)
            proc.kill()
            break
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
