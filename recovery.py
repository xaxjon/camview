#!/usr/bin/env python3
"""Restore the standalone RTSP workaround on UniFi 360 cameras after reboots.

Cameras are selected from streams.json when they are enabled and marked
"dewarp": true. The camera's SSH credentials are supplied by the environment
or /etc/camview/recovery.env through camview-recovery.service; no password is
stored in this repository or passed on a command line.

Useful env overrides: RECOVERY_STREAMS, RECOVERY_POLL_SECONDS,
RECOVERY_FAILURES, RECOVERY_BACKOFF_SECONDS, RECOVERY_RTSP_TIMEOUT,
RECOVERY_SSH_TIMEOUT, UNIFI360_SSH_USER, UNIFI360_SSH_KEY,
UNIFI360_SSH_PASSWORD, UNIFI360_SSH_PASSWORD_FILE.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse, urlunsplit

ROOT = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("RECOVERY_ENV", "/etc/camview/recovery.env"))


def load_env_file(path):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ENV_FILE)

STREAMS = Path(os.environ.get("RECOVERY_STREAMS", ROOT / "streams.json"))
POLL_SECONDS = float(os.environ.get("RECOVERY_POLL_SECONDS", "15"))
FAILURES_NEEDED = int(os.environ.get("RECOVERY_FAILURES", "2"))
BACKOFF_SECONDS = float(os.environ.get("RECOVERY_BACKOFF_SECONDS", "60"))
RTSP_TIMEOUT = float(os.environ.get("RECOVERY_RTSP_TIMEOUT", "5"))
SSH_TIMEOUT = float(os.environ.get("RECOVERY_SSH_TIMEOUT", "120"))

failures = {}
attempted_at = {}

REMOTE_SCRIPT = r'''CFG=/etc/persistent/ubnt_nvr.conf
if [ ! -f "$CFG" ]; then
    echo "camview recovery: $CFG not found" >&2
    exit 1
fi
cp "$CFG" "$CFG.camview-bak" || exit 1

if grep -Eq '^[[:space:]]*\{' "$CFG"; then
    set_json() {
        key=$1
        value=$2
        if grep -q "\"$key\"" "$CFG"; then
            sed -i "s|\"$key\"[[:space:]]*:[[:space:]]*[^,}]*|\"$key\": $value|" "$CFG" || return 1
        else
            tmp="$CFG.camview-tmp"
            cp "$CFG" "$tmp" || return 1
            if awk -v key="$key" -v value="$value" '
                !done && /^[ \t]*\{/ {
                    print
                    print "  \"" key "\": " value ","
                    done=1
                    next
                }
                { print }
            ' "$CFG" > "$tmp"; then
                mv "$tmp" "$CFG" || return 1
            else
                rm -f "$tmp"
                return 1
            fi
        fi
    }
    set_json standaloneEnabled 1 || exit 1
    set_json rtspEnabled 1 || exit 1
    set_json rtspPort "$RTSP_PORT" || exit 1
    set_json rtspAuthEnabled 0 || exit 1
else
    set_kv() {
        key=$1
        value=$2
        if grep -q "^$key=" "$CFG"; then
            sed -i "s|^$key=.*|$key=$value|" "$CFG" || return 1
        else
            echo "$key=$value" >> "$CFG" || return 1
        fi
    }
    set_kv standaloneEnabled 1 || exit 1
    set_kv rtspEnabled 1 || exit 1
    set_kv rtspPort "$RTSP_PORT" || exit 1
    set_kv rtspAuthEnabled 0 || exit 1
fi
sync

pids=$(pidof ubnt_nvr 2>/dev/null || true)
if [ -n "$pids" ]; then
    kill $pids || true
elif [ -x /bin/ubnt_nvr ]; then
    /bin/ubnt_nvr > /dev/null 2>&1 &
fi
sleep 2

ok=0
i=0
while [ "$i" -lt 45 ]; do
    streams=$(curl -fsS http://127.0.0.1:7777/listStreams 2>/dev/null || true)
    if printf '%s\n' "$streams" | grep -q '"s0"' \
       && printf '%s\n' "$streams" | grep -q '"s1"' \
       && printf '%s\n' "$streams" | grep -q '"s2"'; then
        ok=1
        break
    fi
    sleep 1
    i=$((i + 1))
done
if [ "$ok" -ne 1 ]; then
    echo "camview recovery: s0/s1/s2 did not become ready" >&2
    exit 1
fi

services=$(curl -fsS http://127.0.0.1:7777/listServices 2>/dev/null || true)
if ! (printf '%s\n' "$services" | grep -q 'inboundRtsp' \
      && printf '%s\n' "$services" | grep -E "\"port\"[[:space:]]*:[[:space:]]*\"?$RTSP_PORT\"?" > /dev/null); then
    response=$(curl -fsS "http://127.0.0.1:7777/createService?params=$CREATE_PARAMS") || exit 1
    if ! printf '%s\n' "$response" | grep -Eq '"status"[[:space:]]*:[[:space:]]*"SUCCESS"'; then
        echo "camview recovery: createService failed: $response" >&2
        exit 1
    fi
fi
curl -fsS 'http://127.0.0.1:7777/setAuthentication?params=ZW5hYmxlZD1mYWxzZQ==' > /dev/null || exit 1
echo "camview recovery: standalone RTSP restored on port $RTSP_PORT"
'''


def log(message):
    print(f"recovery: {message}", file=sys.stderr, flush=True)


def camera_target(entry):
    if not isinstance(entry, dict) or not entry.get("dewarp") or entry.get("enabled") is False:
        return None
    source = entry.get("source")
    name = entry.get("name")
    if not source or not name:
        return None
    parsed = urlparse(source)
    if parsed.scheme != "rtsp" or not parsed.hostname:
        raise ValueError(f"{name}: source is not an rtsp:// URL")
    return {
        "name": name,
        "host": parsed.hostname,
        "port": parsed.port or 554,
        "path": parsed.path or "/",
        "query": parsed.query,
    }


def load_targets():
    try:
        data = json.loads(STREAMS.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        log(f"cannot read {STREAMS}: {e}")
        return []
    targets = []
    for entry in data:
        try:
            target = camera_target(entry)
        except (ValueError, TypeError) as e:
            log(str(e))
            continue
        if target:
            targets.append(target)
    return targets


def rtsp_url(cam):
    host = f"[{cam['host']}]" if ":" in cam["host"] else cam["host"]
    netloc = host if cam["port"] == 554 else f"{host}:{cam['port']}"
    return urlunsplit(("rtsp", netloc, cam["path"], cam["query"], ""))


def rtsp_healthy(cam, timeout=RTSP_TIMEOUT):
    request = (
        f"DESCRIBE {rtsp_url(cam)} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "Accept: application/sdp\r\n"
        "\r\n"
    ).encode()
    try:
        with socket.create_connection((cam["host"], cam["port"]), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)
            response = b""
            while b"\r\n\r\n" not in response and len(response) < 65536:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                response += chunk
    except OSError:
        return False
    first = response.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    return first.startswith("RTSP/1.0 200") or first.startswith("RTSP/2.0 200")


def ssh_password():
    password = os.environ.get("UNIFI360_SSH_PASSWORD")
    if password:
        return password
    path = os.environ.get("UNIFI360_SSH_PASSWORD_FILE")
    if not path:
        return None
    try:
        return Path(path).read_text().strip()
    except OSError as e:
        raise RuntimeError(f"cannot read UNIFI360_SSH_PASSWORD_FILE: {e}") from e


def ssh_command(cam):
    user = os.environ.get("UNIFI360_SSH_USER", "ubnt")
    ssh = os.environ.get("UNIFI360_SSH_BINARY", "ssh")
    password = ssh_password()
    env = os.environ.copy()
    if password:
        env["SSHPASS"] = password
        cmd = [os.environ.get("SSHPASS_BINARY", "sshpass"), "-e", ssh,
               "-o", "PreferredAuthentications=password",
               "-o", "PubkeyAuthentication=no"]
    else:
        cmd = [ssh, "-o", "BatchMode=yes"]
    cmd += ["-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
    key = os.environ.get("UNIFI360_SSH_KEY")
    if key:
        cmd += ["-i", key, "-o", "IdentitiesOnly=yes"]
    cmd += [f"{user}@{cam['host']}", "sh -s"]
    return cmd, env


def remote_script(port):
    params = base64.b64encode(
        f"ip=0.0.0.0 protocol=inboundRtsp port={port}".encode()).decode()
    return f"RTSP_PORT={int(port)}\nCREATE_PARAMS={params}\n" + REMOTE_SCRIPT


def recover(cam):
    log(f"{cam['name']}: RTSP unhealthy; attempting SSH recovery at {cam['host']}")
    try:
        cmd, env = ssh_command(cam)
        proc = subprocess.run(
            cmd,
            input=remote_script(cam["port"]),
            text=True,
            capture_output=True,
            timeout=SSH_TIMEOUT,
            env=env,
            check=False,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        log(f"{cam['name']}: SSH recovery could not run: {e}")
        return False
    output = "\n".join(x for x in (proc.stdout.strip(), proc.stderr.strip()) if x)
    if proc.returncode != 0:
        log(f"{cam['name']}: SSH recovery failed (rc={proc.returncode}): {output}")
        return False
    if output:
        log(f"{cam['name']}: {output}")
    time.sleep(2)
    if rtsp_healthy(cam):
        log(f"{cam['name']}: RTSP recovery verified")
        return True
    log(f"{cam['name']}: SSH steps completed but RTSP is still not answering")
    return False


def check_once(now=None):
    now = time.monotonic() if now is None else now
    targets = load_targets()
    if not targets:
        return True
    all_healthy = True
    for cam in targets:
        if rtsp_healthy(cam):
            failures.pop(cam["name"], None)
            continue
        failures[cam["name"]] = failures.get(cam["name"], 0) + 1
        count = failures[cam["name"]]
        log(f"{cam['name']}: RTSP health check failed ({count}/{FAILURES_NEEDED})")
        if count < FAILURES_NEEDED:
            all_healthy = False
            continue
        last = attempted_at.get(cam["name"], 0)
        if now - last < BACKOFF_SECONDS:
            all_healthy = False
            continue
        attempted_at[cam["name"]] = now
        if recover(cam):
            failures.pop(cam["name"], None)
        else:
            all_healthy = False
    return all_healthy


def main():
    global FAILURES_NEEDED
    if "--once" in sys.argv:
        FAILURES_NEEDED = 1  # manual recovery should act on the first failed check
        return 0 if check_once() else 1
    log(f"watching {STREAMS} every {POLL_SECONDS:.0f}s")
    while True:
        check_once()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
