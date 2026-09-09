#!/usr/bin/env python3
"""Motion detection supervisor.

Watches streams.json and runs one ffmpeg per camera that has "motion": true
(and is not disabled). Each ffmpeg decodes keyframes only (-skip_frame nokey,
cheap) and writes a JPEG whenever the frame-to-frame scene score exceeds the
camera's threshold:

    motion/<cam>/<YYYY-MM-DD>/<cam>-<Ymd-His>.jpg

Per-camera options in streams.json:
    "motion": true            enable detection
    "motion_threshold": 0.05  scene score 0..1 (higher = less sensitive)
    "motion_source": "rtsp://.../ch1"  low-res substream for near-zero CPU
                              (keeps 1fps granularity; skips keyframe-only mode)
    "motion_zone": [x,y,w,h]  normalized 0..1 fractions of the frame; only
                              motion inside the rectangle triggers capture,
                              but the captured JPEG is always the full frame

Self-healing: ffmpeg gets the RTSP -timeout option so a dead socket makes it
exit on its own, and the supervisor watches each child's *decoded-frame*
liveness: a `showinfo` filter logs one line per decoded frame on stderr, and
a child that decodes nothing for MOTION_STALL_TIMEOUT seconds (frozen
camera, half-open connection, encoder stuck sending undecodable data) is
killed and restarted with exponential backoff. This is independent of scene
motion, so idle cameras are never mistaken for stalled ones. (ffmpeg's
-progress output is timer-driven and useless for this; socket reads do not
show up in /proc io; CPU time cannot tell "decoding" from "stuck parsing
garbage" — decoded frames are the only signal that covers every stall mode.)

Env overrides (used by tests): MOTION_DIR, MOTION_RETENTION_DAYS,
MOTION_POLL_INTERVAL, MOTION_STALL_TIMEOUT, MOTION_TIMEOUT_US.
"""
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FFMPEG = ROOT / "bin" / "ffmpeg"
STREAMS = ROOT / "streams.json"
MOTION_DIR = Path(os.environ.get("MOTION_DIR", ROOT / "motion"))
DEFAULT_THRESHOLD = 0.05
RETENTION_DAYS = int(os.environ.get("MOTION_RETENTION_DAYS", "7"))
POLL_INTERVAL = float(os.environ.get("MOTION_POLL_INTERVAL", "30"))
RESTART_DELAY = 10
STALL_TIMEOUT = float(os.environ.get("MOTION_STALL_TIMEOUT", "120"))
TIMEOUT_US = os.environ.get("MOTION_TIMEOUT_US", "15000000")  # RTSP socket I/O

children = {}      # name -> Popen
running_cfg = {}   # name -> config signature of the running process
restarted_at = {}  # name -> monotonic time of last (re)start
last_frame = {}    # name -> monotonic time of last decoded frame (showinfo)
fails = {}         # name -> consecutive fast-failure count (backs off retries)
shutdown = False


def log(msg):
    print(f"motion: {msg}", file=sys.stderr, flush=True)


def parse_zone(z):
    """[x, y, w, h] as 0..1 floats, or None."""
    if not isinstance(z, (list, tuple)) or len(z) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in z)
    except (TypeError, ValueError):
        return None
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1.0001 or y + h > 1.0001:
        return None
    return (x, y, min(w, 1.0 - x), min(h, 1.0 - y))


def load_config():
    """name -> (source, threshold, use_skip_frame, zone) for motion cameras."""
    try:
        data = json.loads(STREAMS.read_text())
    except Exception as e:
        log(f"cannot read streams.json: {e}")
        return {}
    out = {}
    for s in data:
        if not isinstance(s, dict) or not s.get("name") or not s.get("source"):
            continue
        if s.get("enabled") is False or not s.get("motion"):
            continue
        sub = s.get("motion_source")
        out[s["name"]] = (
            sub or s["source"],
            s.get("motion_threshold", DEFAULT_THRESHOLD),
            not sub,  # keyframe-only mode only when pulling the main stream
            parse_zone(s.get("motion_zone")),
        )
    return out


def ffmpeg_cmd(name, source, threshold, skip_frame, zone):
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "info",
           "-timeout", TIMEOUT_US, "-rtsp_transport", "tcp"]
    if skip_frame:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-i", source]
    if zone:
        x, y, w, h = zone
        # Detection runs on the cropped zone; the trigger frames are padded
        # back to full-canvas size and a framesync'd overlay lays the
        # untouched full-res branch on top — so the gate (select) follows
        # the zone, but the saved JPEG is the full frame at the exact
        # trigger timestamp. showinfo (before split) feeds the watchdog.
        crop = (f"crop=max(floor(iw*{w:.6f}/2)*2\\,2):max(floor(ih*{h:.6f}/2)*2\\,2)"
                f":floor(iw*{x:.6f}/2)*2:floor(ih*{y:.6f}/2)*2")
        pad = (f"pad=floor(iw/{w:.6f}):floor(ih/{h:.6f})"
               f":floor(-iw*{x:.6f}/{w:.6f}):floor(-ih*{y:.6f}/{h:.6f}):black")
        fc = (f"[0:v]scale=480:-1,showinfo,split=2[full][det];"
              f"[det]{crop},select='gt(scene,{threshold})',{pad}[trig];"
              f"[trig][full]overlay=0:0:repeatlast=0[out]")
        cmd += ["-filter_complex", fc, "-map", "[out]"]
    else:
        # showinfo logs one stderr line per decoded frame -> the supervisor's
        # liveness signal; it sits before select so it sees every frame,
        # not just motion frames
        cmd += ["-vf", f"scale=480:-1,showinfo,select='gt(scene,{threshold})'"]
    cmd += [
        "-vsync", "vfr", "-strftime", "1",
        str(MOTION_DIR / name / "%Y-%m-%d" / f"{name}-%Y%m%d-%H%M%S.jpg"),
    ]
    return cmd


def stop_child(name, sig=signal.SIGINT):
    p = children.pop(name, None)
    if not p:
        return
    if p.poll() is None:
        try:
            p.send_signal(sig)
            p.wait(timeout=3)
        except Exception:
            p.kill()
    if p.stderr:
        p.stderr.close()
    last_frame.pop(name, None)


def reconcile():
    wanted = load_config()
    today = date.today().isoformat()

    # stop removed / changed cameras
    for name in list(children):
        if wanted.get(name) != running_cfg.get(name):
            log(f"stopping {name} (config changed or removed)")
            stop_child(name)
            running_cfg.pop(name, None)

    # start / restart cameras
    now = time.monotonic()

    # kill stalled children: any live stream decodes frames constantly (even
    # an idle camera decodes keyframes), so no decoded frame for
    # STALL_TIMEOUT means the child is stuck while still "running"
    for name, p in list(children.items()):
        if p.poll() is not None:
            continue
        if now - last_frame.get(name, restarted_at.get(name, now)) > STALL_TIMEOUT:
            fails[name] = fails.get(name, 0) + 1
            log(f"{name}: no decoded frames for {STALL_TIMEOUT:.0f}s — killing stalled ffmpeg (stall #{fails[name]})")
            stop_child(name, signal.SIGKILL)  # a blocked read can ignore SIGINT
            running_cfg.pop(name, None)

    for name, cfg in wanted.items():
        (MOTION_DIR / name / today).mkdir(parents=True, exist_ok=True)
        p = children.get(name)
        if p is not None and p.poll() is None:
            continue
        if p is not None:
            # a process that ran for >5min was healthy; reset its backoff
            runtime = now - restarted_at.get(name, 0)
            fails[name] = 0 if runtime > 300 else fails.get(name, 0) + 1
            log(f"{name} exited (rc={p.returncode}), will restart")
            children.pop(name, None)
            if p.stderr:
                p.stderr.close()
        # exponential backoff for repeatedly failing cameras (10s -> 5min max)
        delay = min(300, RESTART_DELAY * (1 << fails.get(name, 0)))
        if now - restarted_at.get(name, 0) < delay:
            continue
        source, threshold, skip_frame, zone = cfg
        children[name] = subprocess.Popen(
            ffmpeg_cmd(name, source, threshold, skip_frame, zone),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,  # showinfo frame lines -> watchdog liveness
        )
        running_cfg[name] = cfg
        restarted_at[name] = now
        last_frame[name] = now
        log(f"started {name} (threshold={threshold}, skip_frame={skip_frame}, "
            f"zone={zone if zone else 'full'}, retry_delay={delay}s)")


def drain(timeout):
    """Sleep up to `timeout` seconds while consuming child stderr.

    Keeps the pipes from filling (a blocked ffmpeg would look stalled) and
    feeds the watchdog: every `showinfo` line marks that child as alive.
    All other output is forwarded to our own stderr (the service log).
    """
    fds = {}  # fileno -> camera name
    for name, p in children.items():
        if p.poll() is None and p.stderr:
            fds[p.stderr.fileno()] = name
    if not fds:
        time.sleep(timeout)
        return
    bufs = {}  # fileno -> partial line
    end = time.monotonic() + timeout
    while fds:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        try:
            ready, _, _ = select.select(list(fds), [], [], remaining)
        except OSError:
            return
        if not ready:
            return
        for f in ready:
            name = fds[f]
            try:
                data = os.read(f, 65536)
            except OSError:
                data = b""
            if not data:
                del fds[f]  # EOF: process exiting, reconcile() will reap it
                tail = bufs.pop(f, b"")
                if tail and b"showinfo" not in tail:
                    sys.stderr.buffer.write(tail + b"\n")
                    sys.stderr.buffer.flush()
                continue
            if b"showinfo" in data:
                last_frame[name] = time.monotonic()
            buf = bufs.get(f, b"") + data
            *lines, bufs[f] = buf.split(b"\n")
            fwd = b"".join(l + b"\n" for l in lines if b"showinfo" not in l)
            if fwd:
                sys.stderr.buffer.write(fwd)
                sys.stderr.buffer.flush()


def prune():
    cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    for cam_dir in MOTION_DIR.iterdir() if MOTION_DIR.is_dir() else []:
        if not cam_dir.is_dir():
            continue
        for day_dir in cam_dir.iterdir():
            if not day_dir.is_dir():
                continue
            try:
                if date.fromisoformat(day_dir.name) < cutoff:
                    shutil.rmtree(day_dir, ignore_errors=True)
                    log(f"pruned {day_dir}")
            except ValueError:
                continue


def on_signal(*_):
    global shutdown
    shutdown = True


def main():
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    MOTION_DIR.mkdir(parents=True, exist_ok=True)
    ht = MOTION_DIR / ".htaccess"
    if not ht.exists():
        ht.write_text("Require all denied\n")

    last_prune = 0.0
    log(f"watching {STREAMS} -> {MOTION_DIR} (retention {RETENTION_DAYS}d)")
    while not shutdown:
        reconcile()
        if time.monotonic() - last_prune > 3600:
            prune()
            last_prune = time.monotonic()
        drain(POLL_INTERVAL)

    for name in list(children):
        stop_child(name)
    log("stopped")


if __name__ == "__main__":
    main()
