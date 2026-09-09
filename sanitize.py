#!/usr/bin/env python3
"""RunOnDemand wrapper: re-publish a camera stream unchanged (-c copy).

Usage: sanitize.py <rtsp-source> <rtsp-publish-url>

Why this hop exists: MediaMTX's pull source is strict — one malformed RTP
packet (weak-WiFi cameras emit plenty) stops the source and tears down
EVERY reader of the path at once (motion detector, capturer, transcoder,
viewers). ffmpeg's depacketizers are forgiving: they log and skip garbage
and re-packetize cleanly on the way out. With this in between, the rest of
the system only ever sees a well-formed local stream, and the camera holds
exactly one upstream session.

Same CPU-time stall watchdog as transcode.py: ffmpeg stuck in a blocked
read burns zero CPU; if the counter does not advance for STALL_TIMEOUT
seconds it is killed and MediaMTX (runOnDemandRestart) starts a fresh one.
(ffmpeg's -progress output is timer-driven and useless for this.)
"""
import os
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
            "-timeout", "15000000", "-rtsp_transport", "tcp", "-i", source,
            "-map", "0:v", "-map", "0:a?", "-c", "copy",
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
            print(f"sanitize: no CPU activity for {STALL_TIMEOUT}s, killing ffmpeg", file=sys.stderr)
            proc.kill()
            break
    sys.exit(proc.wait())


if __name__ == "__main__":
    main()
