#!/usr/bin/env python3
"""UI-level test: drives the camview pages in headless Chrome via CDP.

Expects the app served at http://127.0.0.1:8099/ (php -S) with MTX_API set,
a fresh install (no users.json), and the fake AAC camera publishing at
rtsp://127.0.0.1:18554/test.
"""
import asyncio
import json
import os
import subprocess
import time
import urllib.request

import websockets

BASE = f"http://127.0.0.1:{os.environ.get('CAMVIEW_TEST_PORT', '8099')}"
CDP_PORT = 9250

chrome = subprocess.Popen(
    ["google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
     "--disable-dev-shm-usage", f"--remote-debugging-port={CDP_PORT}",
     "--autoplay-policy=no-user-gesture-required", "--window-size=1600,900", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
time.sleep(2)

passed = failed = 0
console_log = []

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
                m = resp.get("method")
                if m == "Runtime.exceptionThrown":
                    console_log.append("PAGE-EXCEPTION: " + str(resp["params"])[:250])
                elif m == "Runtime.consoleAPICalled" and resp["params"].get("type") == "error":
                    console_log.append("CONSOLE.ERROR: " + str(resp["params"])[:250])

        async def js(expr):
            r = await send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
            res = r.get("result", {})
            if "exceptionDetails" in res:
                return f"JSERR: {str(res['exceptionDetails'])[:200]}"
            return res.get("result", {}).get("value")

        await send("Runtime.enable")
        await send("Page.enable")
        async def nav(path):
            await send("Page.navigate", {"url": f"{BASE}/{path}"})
            await asyncio.sleep(1.2)

        async def wait_enabled(expected):
            """Poll the server until testcam's enabled flag matches (toggle PUTs are async)."""
            want = "true" if expected else "false"
            for _ in range(15):
                r = await send("Runtime.evaluate", {"expression": """(async function(){
                  const j = await (await fetch('api/cameras.php?all=1')).json();
                  const c = j.find(x => x.name === 'testcam');
                  return c ? String(c.enabled) : 'missing';
                })()""", "returnByValue": True, "awaitPromise": True})
                v = r.get("result", {}).get("result", {}).get("value")
                if v == want:
                    return v
                await asyncio.sleep(1)
            return v

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
          document.getElementById('f-motion').checked = true;
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

        print("== status probe + hover preview ==")
        dot = None
        for _ in range(20):
            await asyncio.sleep(1)
            dot = await js("""(function(){
              const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam'));
              return row ? row.querySelector('.dot').className : '';
            })()""")
            if dot and 'online' in dot:
                break
        check("probe marks testcam dot green", bool(dot) and 'online' in dot, dot)
        dead = await js("""(function(){
          const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('front-door'));
          return row ? row.querySelector('.dot').className : '';
        })()""")
        check("probe marks unreachable camera red", bool(dead) and 'offline' in dead, dead)
        rect2 = await js("""(function(){
          const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam'));
          const r = row.cells[1].getBoundingClientRect();
          return {x: r.left + 10, y: r.top + r.height / 2};
        })()""")
        await send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": rect2["x"], "y": rect2["y"]})
        await asyncio.sleep(1.5)
        pv = await js("""({
          display: document.getElementById('cam-preview').style.display,
          src: document.getElementById('cam-preview').querySelector('img').src
        })""")
        check("hover shows 480px preview", pv and pv.get("display") == "block" and "snapshot.php?preview=" in (pv.get("src") or ""), pv)
        check("preview is 480px wide", await js("document.getElementById('cam-preview').offsetWidth") == 480)
        await send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 20, "y": 300})
        await asyncio.sleep(0.5)
        check("motion badge in table", await js(
            "[...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam')).textContent.includes('motion')"))

        print("== motion sensitivity slider ==")
        await js("[...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam')).querySelector('[data-act=edit]').click()")
        await asyncio.sleep(0.5)
        check("slider visible when motion enabled", await js("document.getElementById('motion-sens-row').style.display") == "block")
        check("slider defaults to 5", await js("document.getElementById('f-sens').value") == "5")
        await js("document.getElementById('f-sens').value = '7'; document.getElementById('f-sens').dispatchEvent(new Event('input'))")
        await js("document.getElementById('save').click()")
        await asyncio.sleep(2)
        r = await send("Runtime.evaluate", {"expression": """(async function(){
          const r = await fetch('api/cameras.php?all=1');
          const j = await r.json();
          const c = j.find(x => x.name === 'testcam') || {};
          return JSON.stringify({thr: c.motion_threshold, motion: c.motion});
        })()""", "returnByValue": True, "awaitPromise": True})
        srv = r.get("result", {}).get("result", {}).get("value")
        diag = await js("document.querySelectorAll('#rows tr').length + ' res=' + document.getElementById('test-result').textContent + ' err=' + document.getElementById('err').textContent") + ' srv=' + str(srv)
        click2 = await js("""(function(){
          const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam'));
          if (!row) return 'NO ROW';
          row.querySelector('[data-act=edit]').click();
          return 'clicked';
        })()""")
        await asyncio.sleep(0.5)
        val = await js("document.getElementById('f-sens') ? document.getElementById('f-sens').value : 'NO SENS'")
        check("slider round-trips to 7", val == "7", (diag, click2, val))
        await js("document.getElementById('cancel').click()")

        print("== motion timeline page ==")
        await nav("motion.html")
        await asyncio.sleep(2)
        lanes = await js("document.querySelectorAll('.lane').length + ' err=' + document.getElementById('err').textContent + ' @' + location.pathname")
        check("motion page shows one lane per motion camera", lanes == "2 err= @/motion.html", lanes)
        thumbs = await js("document.querySelectorAll('.lane .camthumb').length")
        check("lanes have camera thumbnails", thumbs == 2, thumbs)
        tsrc = await js("[...document.querySelectorAll('.lane')].find(l => l.textContent.includes('testcam')).querySelector('.camthumb').src")
        check("thumbnail is latest motion jpeg", isinstance(tsrc, str) and "motion.php?file=" in tsrc, tsrc)
        # hover the rightmost bucket that has files in the testcam lane
        pt = await js("""(function(){
          const m = buckets['testcam'];
          if (!m || !m.size) return null;
          const col = Math.max(...m.keys());
          const cv = [...document.querySelectorAll('.lane canvas')].find(c => c.dataset.cam === 'testcam');
          const r = cv.getBoundingClientRect();
          return {x: r.left + (col + 0.5) / COLS * r.width, y: r.top + r.height / 2};
        })()""")
        if pt:
            await send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": pt["x"], "y": pt["y"]})
            await asyncio.sleep(1.5)
        disp = await js("document.getElementById('thumb').style.display")
        check("hover flashes thumbnail", disp == "block", disp)

        print("== frame modal, timelapse, timeline zoom ==")
        await send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": pt["x"], "y": pt["y"], "button": "left", "clickCount": 1})
        await send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": pt["x"], "y": pt["y"], "button": "left", "clickCount": 1})
        await asyncio.sleep(1)
        check("bucket click opens modal", await js("document.getElementById('modal-bg').classList.contains('open')"))
        src1 = await js("document.getElementById('fimg').src")
        check("modal shows a motion jpeg", isinstance(src1, str) and "motion.php%3Ffile%3D" in src1.replace("?", "%3F") or "motion.php?file=" in (src1 or ""), src1)
        pos1 = await js("document.getElementById('fpos').textContent")
        await js("document.getElementById('next').click()")
        await asyncio.sleep(0.5)
        pos2 = await js("document.getElementById('fpos').textContent")
        check("next arrow advances frame", pos2 != pos1, (pos1, pos2))
        await js("document.getElementById('play').click()")
        await asyncio.sleep(2.5)
        pos3 = await js("document.getElementById('fpos').textContent")
        check("timelapse advances frames", pos3 != pos2, (pos2, pos3))
        await js("document.getElementById('play').click()")
        await js("document.getElementById('close').click()")
        check("modal closes", await js("!document.getElementById('modal-bg').classList.contains('open')"))
        span0 = await js("VIEW.span")
        await send("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": pt["x"], "y": pt["y"], "deltaX": 0, "deltaY": -240})
        await asyncio.sleep(0.5)
        span1 = await js("VIEW.span")
        check("mouse wheel zooms timeline in", isinstance(span1, (int, float)) and span1 < span0, (span0, span1))
        await js("[...document.querySelectorAll('.lane canvas')][0].dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
        await asyncio.sleep(0.3)
        span2 = await js("VIEW.span")
        check("double-click resets zoom", span2 == span0, (span0, span2))

        await nav("admin.html")  # back to the cameras page for the toggle tests

        print("== disable/enable via toggle ==")
        toggled = False
        for _ in range(10):
            toggled = await js("""(function(){
              const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam'));
              if (!row) return false;
              row.querySelector('[data-act=toggle]').click();
              return true;
            })()""")
            if toggled:
                break
            await asyncio.sleep(1)
        check("toggle click landed", bool(toggled), toggled)
        await wait_enabled(False)

        print("== snapshot from tile + snapshots page ==")
        # re-enable first so the tile exists
        await nav("admin.html")
        for _ in range(10):
            ok = await js("""(function(){
              const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam'));
              if (!row) return false;
              row.querySelector('[data-act=toggle]').click();
              return true;
            })()""")
            if ok:
                break
            await asyncio.sleep(1)
        await wait_enabled(True)
        await nav("index.html")
        tiles3 = None
        for _ in range(15):  # viewer can take a few seconds under probe load
            await asyncio.sleep(1)
            tiles3 = await js("document.querySelectorAll('.tile').length")
            if tiles3 == 3:
                break
        check("viewer shows all tiles after re-enable", tiles3 == 3, tiles3)
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
        for _ in range(10):
            ok = await js("""(function(){
              const row = [...document.querySelectorAll('#rows tr')].find(r => r.textContent.includes('testcam'));
              if (!row) return false;
              row.querySelector('[data-act=toggle]').click();
              return true;
            })()""")
            if ok:
                break
            await asyncio.sleep(1)
        await wait_enabled(False)
        await nav("index.html")
        n2 = None
        for _ in range(15):
            await asyncio.sleep(1)
            n2 = await js("document.querySelectorAll('.tile').length + ' @ ' + location.pathname")
            if n2 and n2.startswith("2 "):
                break
        check("disabled camera hidden from viewer", n2 == "2 @ /index.html", n2)

        print("== logout gates the viewer ==")
        await js("document.getElementById('logout').click()")
        await asyncio.sleep(1.5)
        await nav("index.html")
        path = None
        for _ in range(10):
            await asyncio.sleep(1)
            path = await js("location.pathname")
            if path and path.endswith("login.html"):
                break
        check("logged-out viewer redirects to login", bool(path) and path.endswith("login.html"), path)

try:
    asyncio.run(main())
finally:
    chrome.terminate()

if console_log:
    print("\n-- captured console/exceptions --")
    for line in console_log[:10]:
        print(" ", line)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
