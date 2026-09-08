#!/usr/bin/env python3
"""RunOnDemand wrapper: transcode a camera's audio to Opus and republish.

Usage: transcode.py <rtsp-source> <rtsp-publish-url>

Wraps ffmpeg with a stall watchdog. ffmpeg blocked in a network read
ignores MediaMTX's SIGINT and leaks forever (frozen camera, WiFi drop).
This wrapper tracks the child's CPU time (/proc/<pid>/stat): ffmpeg stuck
in a blocked read burns zero CPU, while any live stream keeps it busy. If
the CPU counter does not advance for STALL_TIMEOUT seconds the child is
killed, and MediaMTX (runOnDemandRestart) or the next viewer starts a
fresh one. (ffmpeg's own -progress output is not a usable signal: it is
timer-driven and keeps ticking while the input is frozen.)
"""
import os
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.path.join(ROOT, "bin", "ffmpeg")
STALL_TIMEOUT = 20  # seconds without any CPU activity

proc = None


def jiffies(pid):
    """utime+stime of a process from /proc/<pid>/stat; None if unavailable."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()  # skip (comm), field 3 = rest[0]
        return int(rest[11]) + int(rest[12])           # fields 14+15: utime + stime
    except (OSError, ValueError, IndexError):
        return None


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
            "-rtsp_transport", "tcp", "-f", "rtsp", out,
        ],
        stdout=subprocess.DEVNULL,
        # stderr stays inherited -> ends up in the MediaMTX log
    )

    last_cpu = None
    last_change = time.monotonic()
    while proc.poll() is None:
        time.sleep(2)
        j = jiffies(proc.pid)
        if j is None:
            continue  # process gone or no /proc — plain wait covers exit
        if j != last_cpu:
            last_cpu = j
            last_change = time.monotonic()
        elif time.monotonic() - last_change > STALL_TIMEOUT:
            print(f"transcode: no CPU activity for {STALL_TIMEOUT}s, killing ffmpeg", file=sys.stderr)
            proc.kill()
            break
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
