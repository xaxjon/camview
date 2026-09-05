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

Env overrides (used by tests): MOTION_DIR, MOTION_RETENTION_DAYS,
MOTION_POLL_INTERVAL.
"""
import json
import os
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

children = {}      # name -> Popen
running_cfg = {}   # name -> config signature of the running process
restarted_at = {}  # name -> monotonic time of last (re)start
shutdown = False


def log(msg):
    print(f"motion: {msg}", file=sys.stderr, flush=True)


def load_config():
    """name -> (source, threshold, use_skip_frame) for motion-enabled cameras."""
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
        )
    return out


def ffmpeg_cmd(name, source, threshold, skip_frame):
    vf = f"scale=480:-1,select='gt(scene,{threshold})'"
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error"]
    if skip_frame:
        cmd += ["-skip_frame", "nokey"]
    cmd += [
        "-rtsp_transport", "tcp", "-i", source,
        "-vf", vf, "-vsync", "vfr", "-strftime", "1",
        str(MOTION_DIR / name / "%Y-%m-%d" / f"{name}-%Y%m%d-%H%M%S.jpg"),
    ]
    return cmd


def stop_child(name, sig=signal.SIGINT):
    p = children.pop(name, None)
    if not p or p.poll() is not None:
        return
    try:
        p.send_signal(sig)
        p.wait(timeout=3)
    except Exception:
        p.kill()


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
    for name, cfg in wanted.items():
        (MOTION_DIR / name / today).mkdir(parents=True, exist_ok=True)
        p = children.get(name)
        if p is not None and p.poll() is None:
            continue
        if p is not None:
            log(f"{name} exited (rc={p.returncode}), will restart")
            children.pop(name, None)
        if now - restarted_at.get(name, 0) < RESTART_DELAY:
            continue
        source, threshold, skip_frame = cfg
        children[name] = subprocess.Popen(
            ffmpeg_cmd(name, source, threshold, skip_frame),
            stdout=subprocess.DEVNULL,  # stderr inherited -> service log
        )
        running_cfg[name] = cfg
        restarted_at[name] = now
        log(f"started {name} (threshold={threshold}, skip_frame={skip_frame})")


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
        time.sleep(POLL_INTERVAL)

    for name in list(children):
        stop_child(name)
    log("stopped")


if __name__ == "__main__":
    main()
