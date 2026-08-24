#!/usr/bin/env python3
"""End-to-end test: serve the viewer, load it in headless Chrome,
verify WebRTC playback of the 'test' stream and the fullscreen toggles.

Prerequisites (see README.md "Testing"):
  - MediaMTX running with a publishable path named 'test' (test-mediamtx.yml)
  - an ffmpeg test pattern publishing to rtsp://127.0.0.1:8554/test
"""
import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

CDP_PORT = 9224
WEB_PORT = 8899
URL = f"http://127.0.0.1:{WEB_PORT}/"

webroot = Path(tempfile.mkdtemp(prefix="rtsp-viewer-test-"))
shutil.copy(Path(__file__).parent / "index.html", webroot)
(webroot / "streams.json").write_text(
    json.dumps([{"name": "test", "source": "rtsp://127.0.0.1:8554/test"}])
)
webserver = subprocess.Popen(
    ["python3", "-m", "http.server", str(WEB_PORT)],
    cwd=webroot, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

chrome = subprocess.Popen(
    [
        "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", f"--remote-debugging-port={CDP_PORT}",
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=1600,900", "about:blank",
    ],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep(2)

EVAL = """
(function(){
  const v = document.querySelector('video');
  const status = document.querySelector('.status');
  const tiles = document.querySelectorAll('.tile');
  return {
    tiles: tiles.length,
    gridCols: document.getElementById('grid').style.gridTemplateColumns,
    tileW: tiles.length ? tiles[0].clientWidth : 0,
    tileH: tiles.length ? tiles[0].clientHeight : 0,
    videoWidth: v ? v.videoWidth : 0,
    videoHeight: v ? v.videoHeight : 0,
    readyState: v ? v.readyState : 0,
    paused: v ? v.paused : true,
    statusHidden: status ? status.hidden : null,
    fullscreen: !!document.fullscreenElement,
  };
})()
"""

async def main():
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list") as r:
        page = next(p for p in json.loads(r.read()) if p["type"] == "page")

    async with websockets.connect(page["webSocketDebuggerUrl"]) as ws:
        mid = 0

        async def send(method, params=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == mid:
                    return resp

        async def js(expr):
            r = await send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("result", {}).get("value")

        await send("Page.enable")
        await send("Page.navigate", {"url": URL})
        await asyncio.sleep(6)  # let WHEP connect and video start

        state = await js(EVAL)
        print("PLAYBACK:", json.dumps(state, indent=2))

        # single click -> fullscreen (real trusted mouse events via CDP)
        rect = await js(
            "const r = document.querySelector('.tile').getBoundingClientRect();"
            "({x: r.left + r.width / 2, y: r.top + r.height / 2})"
        )

        async def click(count):
            for kind in ("mousePressed", "mouseReleased"):
                await send("Input.dispatchMouseEvent", {
                    "type": kind, "x": rect["x"], "y": rect["y"],
                    "button": "left", "clickCount": count,
                })

        await click(1)
        await asyncio.sleep(1)
        fs = await js("!!document.fullscreenElement")
        print("AFTER CLICK, fullscreen:", fs)

        # double click -> back to grid
        # (CDP mouse events don't reach the page in headless fullscreen mode,
        #  so dispatch dblclick synthetically; exitFullscreen needs no gesture)
        await js("""
          const t = document.querySelector('.tile');
          t.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
        """)
        await asyncio.sleep(1)
        back = await js("!!document.fullscreenElement")
        print("AFTER DBLCLICK, fullscreen:", back)

        ok = (
            state["tiles"] == 1
            and state["videoWidth"] == 1280
            and state["videoHeight"] == 720
            and state["readyState"] >= 2
            and not state["paused"]
            and state["statusHidden"]
            and fs is True
            and back is False
        )
        print("RESULT:", "PASS" if ok else "FAIL")

try:
    asyncio.run(main())
finally:
    chrome.terminate()
    webserver.terminate()
    shutil.rmtree(webroot, ignore_errors=True)
