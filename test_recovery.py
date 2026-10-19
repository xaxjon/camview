#!/usr/bin/env python3
"""Focused tests for the UniFi 360 recovery supervisor."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import recovery


class FakeRtsp:
    def __init__(self, status="404 Not Found"):
        self.status = status
        self.stopping = threading.Event()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = conn.recv(4096)
                        if not chunk:
                            raise OSError("incomplete RTSP request")
                        request += chunk
                    assert request.startswith(b"DESCRIBE rtsp://127.0.0.1:")
                    body = b"v=0\r\nm=video 0 RTP/AVP 96\r\n" if self.status.startswith("200") else b""
                    response = (
                        f"RTSP/1.0 {self.status}\r\nCSeq: 1\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n"
                    ).encode() + body
                    conn.sendall(response)
                except OSError:
                    pass

    def close(self):
        self.stopping.set()
        self.sock.close()
        self.thread.join(timeout=2)


server = FakeRtsp()
try:
    cam = {"name": "cam360", "host": "127.0.0.1", "port": server.port,
           "path": "/s1", "query": ""}
    assert not recovery.rtsp_healthy(cam, timeout=1)
    server.status = "200 OK"
    assert recovery.rtsp_healthy(cam, timeout=1)
    print("recovery RTSP health: ok")

    with tempfile.TemporaryDirectory(prefix="camview-recovery-") as tmp:
        streams = Path(tmp) / "streams.json"
        streams.write_text(json.dumps([
            {"name": "cam360", "source": f"rtsp://127.0.0.1:{server.port}/s1", "dewarp": True},
            {"name": "disabled", "source": "rtsp://127.0.0.1:1/s1", "dewarp": True, "enabled": False},
            {"name": "normal", "source": "rtsp://127.0.0.1:2/live", "dewarp": False},
        ]))
        recovery.STREAMS = streams
        targets = recovery.load_targets()
        assert [t["name"] for t in targets] == ["cam360"], targets
        assert targets[0]["path"] == "/s1" and targets[0]["port"] == server.port
    print("recovery camera targeting: ok")

    script = recovery.remote_script(8554)
    assert "RTSP_PORT=8554" in script
    assert "aXA9MC4wLjAuMCBwcm90b2NvbD1pbmJvdW5kUnRzcCBwb3J0PTg1NTQ=" in script
    assert "standaloneEnabled" in script and "listStreams" in script
    print("recovery remote script: ok")

    keys = ["UNIFI360_SSH_USER", "UNIFI360_SSH_KEY", "UNIFI360_SSH_PASSWORD",
            "UNIFI360_SSH_PASSWORD_FILE"]
    old_env = {k: os.environ.get(k) for k in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        os.environ["UNIFI360_SSH_PASSWORD"] = "secret-test-password"
        cmd, env = recovery.ssh_command(cam)
        assert cmd[0] == "sshpass" and env["SSHPASS"] == "secret-test-password"
        assert all("secret-test-password" not in str(part) for part in cmd), cmd
        assert "ubnt@127.0.0.1" in cmd and "BatchMode=yes" not in cmd
        os.environ.pop("UNIFI360_SSH_PASSWORD", None)
        os.environ["UNIFI360_SSH_KEY"] = "/tmp/camview-test-key"
        cmd, env = recovery.ssh_command(cam)
        assert cmd[0] == "ssh" and "BatchMode=yes" in cmd
        assert "-i" in cmd and "/tmp/camview-test-key" in cmd
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("recovery SSH credentials: ok")

    with tempfile.TemporaryDirectory(prefix="camview-recovery-env-") as tmp:
        env_file = Path(tmp) / "recovery.env"
        env_file.write_text('UNIFI360_SSH_PASSWORD="from-file"\n')
        env = os.environ.copy()
        env["RECOVERY_ENV"] = str(env_file)
        env.pop("UNIFI360_SSH_PASSWORD", None)
        subprocess.run([
            sys.executable, "-c",
            "import recovery; assert recovery.ssh_password() == 'from-file'",
        ], cwd=Path(__file__).resolve().parent, env=env, check=True)
    print("recovery env file: ok")

    server.status = "404 Not Found"
    backoff_tmp = tempfile.TemporaryDirectory(prefix="camview-recovery-backoff-")
    streams = Path(backoff_tmp.name) / "streams.json"
    streams.write_text(json.dumps([
        {"name": "cam360", "source": f"rtsp://127.0.0.1:{server.port}/s1", "dewarp": True},
    ]))
    recovery.STREAMS = streams
    old = (recovery.FAILURES_NEEDED, recovery.BACKOFF_SECONDS, recovery.recover)
    calls = []
    try:
        recovery.FAILURES_NEEDED = 2
        recovery.BACKOFF_SECONDS = 60
        recovery.failures.clear()
        recovery.attempted_at.clear()
        recovery.recover = lambda c: calls.append(c["name"]) or True
        assert recovery.check_once(now=1000) is False
        assert calls == []
        assert recovery.check_once(now=1015) is True
        assert calls == ["cam360"], calls
        recovery.check_once(now=1030)
        assert calls == ["cam360"], calls
        recovery.attempted_at["cam360"] = 0
        recovery.check_once(now=1091)
        assert calls == ["cam360", "cam360"], calls
    finally:
        (recovery.FAILURES_NEEDED, recovery.BACKOFF_SECONDS, recovery.recover) = old
        recovery.failures.clear()
        recovery.attempted_at.clear()
        backoff_tmp.cleanup()
    print("recovery failure/backoff: ok")
finally:
    server.close()

print("recovery supervisor tests: ok")
