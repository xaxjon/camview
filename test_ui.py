#!/usr/bin/env python3
"""UI-level test: drives the camview pages in headless Chrome via CDP.

Expects the app served at http://127.0.0.1:8099/ (php -S) with MTX_API set,
a fresh install (no users.json), and the fake AAC camera publishing at
rtsp://127.0.0.1:18554/test.
"""
import asyncio
import json
import subprocess
import time
import urllib.request

import websockets

BASE = "http://127.0.0.1:8099"
CDP_PORT = 9250

chrome = subprocess.Popen(
    ["google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
     "--disable-dev-shm-usage", f"--remote-debugging-port={CDP_PORT}",
     "--autoplay-policy=no-user-gesture-required", "--window-size=1600,900", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep(2)

passed = failed = 0

def check(name, cond, extra=""):
    global passed, failed
    if cond: passed += 1; print(f"  ok   {name}")
    else: failed += 1; print(f"  FAIL {name} {extra!r}")

async def main():
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list") as r:
        page = next(p for p in json.loads(r.read()) if p["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"]) as ws:
        mid = 0
        async def send(method, params=None):
            nonlocal mid; mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                resp = json.loads(await ws.recv())
                if resp.get("id") == mid:
                    return resp
        async def js(expr):
            r = await send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            res = r.get("result", {})
            if "exceptionDetails" in res:
                return f"JSERR: {str(res['exceptionDetails'])[:200]}"
            return res.get("result", {}).get("value")
        async def nav(path):
            await send("Page.navigate", {"url": f"{BASE}/{path}"})
            await asyncio.sleep(1.2)

        print("== first-run flow ==")
        await nav("index.html")
        check("index redirects to setup", (await js("location.pathname")).endswith("setup.html"))
        await js("""
          document.getElementById('username').value = 'admin';
          document.getElementById('password').value = 'adminpass1';
          document.getElementById('password2').value = 'adminpass1';
          document.getElementById('form').dispatchEvent(new Event('submit', {cancelable: true}));
        """)
        await asyncio.sleep(1.5)
        check("setup redirects to login", (await js("location.pathname")).endswith("login.html"))
        await js("""
          document.getElementById('username').value = 'admin';
          document.getElementById('password').value = 'adminpass1';
          document.getElementById('form').dispatchEvent(new Event('submit', {cancelable: true}));
        """)
        await asyncio.sleep(2)
        check("login lands on viewer", (await js("location.pathname")).endswith("index.html"))
        n = await js("document.querySelectorAll('.tile').length")
        check("grid shows example cameras", n == 2, n)
        check("admin link visible", await js("!document.getElementById('admin-link').hidden"))
        check("tiles have status dots", await js("document.querySelectorAll('.tile .dot').length") == 2)
        check("tiles have snapshot buttons", await js("document.querySelectorAll('.tile .snap').length") == 2)

        print("== admin: add camera with live test ==")
        await nav("admin.html")
        check("admin page loads", (await js("document.querySelectorAll('#rows tr').length")) == 2)
        await js("document.getElementById('add').click()")
        await asyncio.sleep(0.3)
        await js("""
          document.getElementById('f-name').value = 'testcam';
          document.getElementById('f-source').value = 'rtsp://127.0.0.1:18554/test';
          document.getElementById('f-transcode').checked = true;
        """)
        await js("document.getElementById('test').click()")
        for _ in range(30):
            await asyncio.sleep(1)
            t = await js("document.getElementById('test-result').textContent")
            if t and 'testing' not in t:
                break
        check("live test reports OK + H264", isinstance(t, str) and "OK" in t and "H264" in t, t)
        check("AAC hint shown", isinstance(t, str) and "transcode" in t, t)
        await js("document.getElementById('save').click()")
        await asyncio.sleep(2)
        check("camera appears in table", (await js(
            "[...document.querySelectorAll('#rows tr td:nth-child(2)')].map(td=>td.textContent)"))
            == ["front-door", "driveway", "testcam"])

        print("== disable/enable via toggle ==")
        await js("[...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam')).querySelector('[data-act=toggle]').click()")
        await asyncio.sleep(2)

        print("== snapshot from tile + snapshots page ==")
        # re-enable first so the tile exists
        await nav("admin.html")
        await js("[...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam')).querySelector('[data-act=toggle]').click()")
        await asyncio.sleep(2)
        await nav("index.html")
        await js("[...document.querySelectorAll('.tile')].find(t => t.dataset.name === 'testcam').querySelector('.snap').click()")
        toast = None
        for _ in range(20):
            await asyncio.sleep(1)
            toast = await js("document.querySelector('.tile[data-name=testcam] .toast')?.textContent || ''")
            if toast:
                break
        check("tile snapshot saved with toast", isinstance(toast, str) and toast.startswith("saved testcam-"), toast)
        await nav("snapshots.html")
        await asyncio.sleep(2)
        check("snapshots page lists the file", (await js(
            "[...document.querySelectorAll('#grid .card b')].map(b => b.textContent)")) == ["testcam"])
        check("purge button visible to admin", await js("!document.getElementById('purge').hidden"))

        print("== disable hides camera from grid ==")
        await nav("admin.html")
        await js("[...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam')).querySelector('[data-act=toggle]').click()")
        await asyncio.sleep(2)
        await nav("index.html")
        n2 = await js("document.querySelectorAll('.tile').length + ' @ ' + location.pathname")
        check("disabled camera hidden from viewer", n2 == "2 @ /index.html", n2)

        print("== logout gates the viewer ==")
        await js("document.getElementById('logout').click()")
        await asyncio.sleep(1.5)
        await nav("index.html")
        check("logged-out viewer redirects to login", (await js("location.pathname")).endswith("login.html"))

try:
    asyncio.run(main())
finally:
    chrome.terminate()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
