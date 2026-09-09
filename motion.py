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

Zoned cameras run TWO ffmpeg processes (a single-process split/overlay
graph wedges — ffmpeg 7's scheduler stalls when two outputs run at
different rates, and framesync buffers the dense branch unboundedly):
  - detector: keyframe-only, crops to the zone, writes small trigger JPEGs
    to motion/<cam>/.trig/<Ymd-His>.jpg when the zone's scene score fires
  - capturer: keyframe-only full frames, rewrites motion/<cam>/.latest.jpg
    via image2 -update 1 (about one frame per keyframe interval)
The supervisor copies .latest.jpg into the timeline under the trigger's
timestamp and deletes the trigger (checked roughly once a second).

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

children = {}      # name -> {"det": Popen, "cap": Popen|None} (cap = zoned only)
running_cfg = {}   # name -> config signature of the running process
restarted_at = {}  # name -> monotonic time of last (re)start
last_frame = {}    # (name, role) -> monotonic time of last decoded frame (showinfo)
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
    """name -> (det_source, main_source, threshold, det_skip_frame, zone)."""
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
            sub or s["source"],   # detector pulls the substream when set
            s["source"],          # capturer always pulls the main stream
            s.get("motion_threshold", DEFAULT_THRESHOLD),
            not sub,              # keyframe-only detection only on main stream
            parse_zone(s.get("motion_zone")),
        )
    return out


def base_cmd(source, skip_frame):
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "info",
           "-timeout", TIMEOUT_US, "-rtsp_transport", "tcp"]
    if skip_frame:
        cmd += ["-skip_frame", "nokey"]
    return cmd + ["-i", source]


def detector_cmd(name, source, threshold, skip_frame, zone):
    # showinfo logs one stderr line per decoded frame -> the supervisor's
    # liveness signal; it sits before select so it sees every frame,
    # not just motion frames
    vf = f"scale=480:-1,showinfo,select='gt(scene,{threshold})'"
    out = str(MOTION_DIR / name / "%Y-%m-%d" / f"{name}-%Y%m%d-%H%M%S.jpg")
    if zone:
        x, y, w, h = zone
        crop = (f"crop=max(floor(iw*{w:.6f}/2)*2\\,2):max(floor(ih*{h:.6f}/2)*2\\,2)"
                f":floor(iw*{x:.6f}/2)*2:floor(ih*{y:.6f}/2)*2")
        vf = f"scale=480:-1,{crop},showinfo,select='gt(scene,{threshold})'"
        out = str(MOTION_DIR / name / ".trig" / "%Y%m%d-%H%M%S.jpg")
    return base_cmd(source, skip_frame) + [
        "-vf", vf, "-vsync", "vfr", "-strftime", "1", out,
    ]


def capturer_cmd(name, main_source):
    # full frame, rewritten in place ~once per keyframe interval
    return base_cmd(main_source, True) + [
        "-vf", "scale=480:-1,showinfo", "-vsync", "vfr",
        "-update", "1", str(MOTION_DIR / name / ".latest.jpg"),
    ]


def stop_child(name, sig=signal.SIGINT):
    unit = children.pop(name, None)
    if not unit:
        return
    for p in unit.values():
        if p.poll() is None:
            try:
                p.send_signal(sig)
                p.wait(timeout=3)
            except Exception:
                p.kill()
        if p.stderr:
            p.stderr.close()
    for role in unit:
        last_frame.pop((name, role), None)
    # drop zone state files; a zoned config recreates them on start
    shutil.rmtree(MOTION_DIR / name / ".trig", ignore_errors=True)
    try:
        (MOTION_DIR / name / ".latest.jpg").unlink()
    except OSError:
        pass


def start_child(name, cfg, delay):
    det_source, main_source, threshold, skip_frame, zone = cfg
    cam_dir = MOTION_DIR / name
    procs = {}
    if zone:
        (cam_dir / ".trig").mkdir(parents=True, exist_ok=True)
        procs["cap"] = subprocess.Popen(
            capturer_cmd(name, main_source),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    procs["det"] = subprocess.Popen(
        detector_cmd(name, det_source, threshold, skip_frame, zone),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,  # showinfo frame lines -> watchdog liveness
    )
    children[name] = procs
    running_cfg[name] = cfg
    now = time.monotonic()
    restarted_at[name] = now
    for role in procs:
        last_frame[(name, role)] = now
    log(f"started {name} (threshold={threshold}, skip_frame={skip_frame}, "
        f"zone={zone if zone else 'full'}, retry_delay={delay}s)")


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

    # kill stalled units: any live stream decodes frames constantly (even
    # an idle camera decodes keyframes), so no decoded frame for
    # STALL_TIMEOUT on any of the unit's processes means it is stuck while
    # still "running" — restart the whole unit
    for name, unit in list(children.items()):
        stalled = False
        for role, p in unit.items():
            if p.poll() is not None:
                continue
            if now - last_frame.get((name, role), restarted_at.get(name, now)) > STALL_TIMEOUT:
                stalled = True
                break
        if stalled:
            fails[name] = fails.get(name, 0) + 1
            log(f"{name}: no decoded frames for {STALL_TIMEOUT:.0f}s — killing stalled ffmpeg (stall #{fails[name]})")
            stop_child(name, signal.SIGKILL)  # a blocked read can ignore SIGINT
            running_cfg.pop(name, None)

    for name, cfg in wanted.items():
        (MOTION_DIR / name / today).mkdir(parents=True, exist_ok=True)
        unit = children.get(name)
        if unit is not None:
            if all(p.poll() is None for p in unit.values()):
                continue  # healthy
            # a process that ran for >5min was healthy; reset its backoff
            runtime = now - restarted_at.get(name, 0)
            fails[name] = 0 if runtime > 300 else fails.get(name, 0) + 1
            rc = next(p.returncode for p in unit.values() if p.poll() is not None)
            log(f"{name} exited (rc={rc}), will restart")
            stop_child(name)  # reap the rest of the unit
        # exponential backoff for repeatedly failing cameras (10s -> 5min max)
        delay = min(300, RESTART_DELAY * (1 << fails.get(name, 0)))
        if now - restarted_at.get(name, 0) < delay:
            continue
        start_child(name, cfg, delay)


def harvest():
    """Turn zone trigger frames into full-frame timeline captures.

    For each zoned camera: every JPEG in .trig/ is a detection timestamp;
    copy the capturer's .latest.jpg into the timeline under that timestamp.
    """
    for name, unit in children.items():
        if "cap" not in unit:
            continue
        trig = MOTION_DIR / name / ".trig"
        latest = MOTION_DIR / name / ".latest.jpg"
        try:
            triggers = sorted(trig.iterdir())
            data = latest.read_bytes()
        except OSError:
            continue
        if not (data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"):
            continue  # capturer mid-write or not started yet
        for t in triggers:
            ts = t.name[:-4]  # strip .jpg
            try:
                day = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                int(ts.replace("-", ""))  # ts is Ymd-HMS
            except (ValueError, IndexError):
                try:
                    t.unlink()
                except OSError:
                    pass
                continue
            day_dir = MOTION_DIR / name / day
            day_dir.mkdir(parents=True, exist_ok=True)
            dest = day_dir / f"{name}-{ts}.jpg"
            for i in range(2, 10):  # same-second collisions
                if not dest.exists():
                    break
                dest = day_dir / f"{name}-{ts}-{i}.jpg"
            tmp = dest.with_suffix(".tmp")
            try:
                tmp.write_bytes(data)
                tmp.rename(dest)
                log(f"{name}: captured {dest.name}")
            except OSError:
                pass
            try:
                t.unlink()
            except OSError:
                pass


def drain(timeout):
    """Sleep up to `timeout` seconds while consuming child stderr.

    Keeps the pipes from filling (a blocked ffmpeg would look stalled) and
    feeds the watchdog: every `showinfo` line marks that process as alive.
    All other output is forwarded to our own stderr (the service log).
    Wakes about once a second to harvest zone trigger frames.
    """
    def safe_harvest():
        try:
            harvest()
        except Exception as e:
            log(f"harvest error: {e}")

    fds = {}  # fileno -> (camera name, role)
    for name, unit in children.items():
        for role, p in unit.items():
            if p.poll() is None and p.stderr:
                fds[p.stderr.fileno()] = (name, role)
    bufs = {}  # fileno -> partial line
    end = time.monotonic() + timeout
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        safe_harvest()
        if not fds:
            time.sleep(min(remaining, 1.0))
            continue
        try:
            ready, _, _ = select.select(list(fds), [], [], min(remaining, 1.0))
        except OSError:
            return
        for f in ready:
            name, role = fds[f]
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
                last_frame[(name, role)] = time.monotonic()
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
