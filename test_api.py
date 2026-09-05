#!/usr/bin/env python3
"""API-level test for camview backend (setup, auth, CRUD, test, snapshot, users)."""
import json
import pathlib
import re
import urllib.request
import urllib.parse
import http.cookiejar

BASE = "http://127.0.0.1:8099/api"
MTX = "http://127.0.0.1:29997"

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

passed = failed = 0

def check(name, cond, extra=""):
    global passed, failed
    if cond: passed += 1; print(f"  ok   {name}")
    else: failed += 1; print(f"  FAIL {name} {extra}")

def api(path, method="GET", data=None, csrf=None, raw=False):
    req = urllib.request.Request(f"{BASE}/{path}", method=method)
    body = None
    if data is not None:
        req.add_header("Content-Type", "application/json")
        body = json.dumps(data).encode()
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    try:
        with opener.open(req, body) as r:
            return r.status, (r.read() if raw else json.loads(r.read()))
    except urllib.error.HTTPError as e:
        payload = e.read()
        if raw: return e.code, payload
        try: return e.code, json.loads(payload)
        except json.JSONDecodeError: return e.code, {}

def mtx_paths():
    with urllib.request.urlopen(f"{MTX}/v3/paths/list") as r:
        return {i["name"]: i["ready"] for i in json.loads(r.read())["items"]}

print("== first-run setup ==")
s, j = api("me.php"); check("setup_needed on fresh install", s == 200 and j["setup_needed"] is True)
s, j = api("cameras.php"); check("cameras requires login (401)", s == 401)
s, j = api("setup.php", "POST", {"username": "admin", "password": "adminpass1"})
check("create first admin", s == 200, j)
s, j = api("setup.php", "POST", {"username": "x", "password": "whatever1"})
check("setup refuses once users exist", s == 403)

print("== login ==")
s, j = api("login.php", "POST", {"username": "admin", "password": "wrong"})
check("wrong password rejected", s == 401)
s, j = api("login.php", "POST", {"username": "admin", "password": "adminpass1"})
check("admin login", s == 200 and j["role"] == "admin")
csrf = j.get("csrf")
check("csrf token issued", bool(csrf))

print("== camera CRUD + live apply ==")
cam = {"name": "testcam", "source": "rtsp://127.0.0.1:18554/test", "transcode_audio": True, "motion": True}
s, j = api("cameras.php", "POST", cam); check("add without CSRF rejected", s == 403)
s, j = api("cameras.php", "POST", cam, csrf)
check("add camera", s == 200, j)
check("path live in mediamtx", "testcam" in mtx_paths())
s, j = api("cameras.php", "POST", cam, csrf); check("duplicate name rejected", s == 409)
s, j = api("cameras.php", "POST", {"name": "bad name!", "source": "rtsp://x"}, csrf)
check("invalid name rejected", s == 400)
s, j = api("cameras.php", "POST", {"name": "ok", "source": "http://x"}, csrf)
check("non-rtsp source rejected", s == 400)

s, j = api("cameras.php")
row = next((c for c in j if c["name"] == "testcam"), {})
check("admin sees source + status", row.get("source", "").startswith("rtsp://") and "status" in row, row)

print("== motion timeline API ==")
sj = json.loads(pathlib.Path("/tmp/camview-test/streams.json").read_text())
entry = next(e for e in sj if isinstance(e, dict) and e.get("name") == "testcam")
check("motion flag persisted in streams.json", entry.get("motion") is True, entry)
s, j = api("motion.php?range=24h")
check("testcam listed as motion camera", s == 200 and "testcam" in j.get("cams", []), j)
mfiles = j.get("files", {}).get("testcam", [])
check("seeded motion files returned", len(mfiles) == 3, mfiles)
if mfiles:
    rel = mfiles[0][1]
    s, data = api(f"motion.php?file={urllib.parse.quote(rel)}", raw=True)
    check("motion jpeg served", s == 200 and data[:2] == b"\xff\xd8", s)
s, j = api("motion.php?file=..%2f..%2fstreams.json")
check("motion path traversal rejected", s in (400, 404), s)

print("== camera test endpoint ==")
s, j = api("camera-test.php", "POST", {"source": "rtsp://127.0.0.1:18554/test"}, csrf)
check("probe reports H264", s == 200 and j.get("ok") and "H264" in j.get("video", ""), j)
check("AAC hint present", j.get("audio", "").startswith("aac") and "transcode" in (j.get("hint") or ""), j)
s, j = api("camera-test.php", "POST", {"source": "rtsp://127.0.0.1:18554/nope"}, csrf)
check("dead URL reported cleanly", s == 200 and j.get("ok") is False, j)

print("== snapshots: save, list, serve, archive, delete, purge ==")
s, j = api("snapshot.php", "POST", {"name": "testcam"}, csrf)
check("save snapshot", s == 200 and j.get("ok") and re.match(r'^testcam-\d{8}-\d{6}(-\d+)?\.jpg$', j.get("file", "")), j)
snap1 = j.get("file")
s, j = api("snapshot.php", "POST", {"name": "ghost"}, csrf)
check("snapshot unknown cam 404", s == 404)
s, j = api("snapshots.php")
row = next((f for f in j.get("files", []) if f["file"] == snap1), None)
check("snapshot listed with cam name", row and row["cam"] == "testcam" and row["size"] > 1000, j)
s, data = api(f"snapshots.php?file={snap1}", raw=True)
check("serve snapshot JPEG", s == 200 and data[:2] == b"\xff\xd8", s)
s, j = api("snapshots.php?file=..%2fstreams.json")
check("path traversal rejected", s in (400, 404), s)
s, data = api("snapshots.php?action=archive", "POST", {"files": [snap1]}, csrf, raw=True)
check("archive is gzip", s == 200 and data[:2] == b"\x1f\x8b", (s, data[:10]))
s, j = api("snapshots.php", "DELETE", {"files": [snap1]}, csrf)
check("bulk delete", s == 200 and j.get("deleted") == 1, j)
s, j = api("snapshot.php", "POST", {"name": "testcam"}, csrf)
api("snapshot.php", "POST", {"name": "testcam"}, csrf)
s, j = api("snapshots.php", "DELETE", {"all": True}, csrf)
check("purge all", s == 200 and j.get("deleted") == 2, j)
s, j = api("snapshots.php")
check("list empty after purge", j.get("files") == [], j)

print("== disable / enable / delete ==")
s, j = api("cameras.php", "PUT", {"original": "testcam", **cam, "enabled": False}, csrf)
check("disable camera", s == 200, j)
check("disabled path removed from mediamtx", "testcam" not in mtx_paths())
s, j = api("cameras.php", "PUT", {"original": "testcam", **cam, "enabled": True}, csrf)
check("enable camera", s == 200 and "testcam" in mtx_paths(), j)
s, j = api("cameras.php", "DELETE", {"name": "testcam"}, csrf)
check("delete camera", s == 200 and "testcam" not in mtx_paths(), j)

print("== users + roles ==")
s, j = api("users.php", "POST", {"username": "bob", "password": "viewerpass1", "role": "viewer"}, csrf)
check("create viewer user", s == 200, j)
api("cameras.php", "POST", cam, csrf)  # re-add for viewer tests
s, j = api("users.php", "DELETE", {"username": "admin"}, csrf)
check("cannot delete yourself", s == 400)
s, j = api("logout.php", "POST")
s, j = api("login.php", "POST", {"username": "bob", "password": "viewerpass1"})
check("viewer login", s == 200 and j["role"] == "viewer")
vcsrf = j.get("csrf")
s, j = api("cameras.php")
check("viewer sees cameras without source", s == 200 and j and "source" not in j[0], j)
s, j = api("cameras.php", "POST", {"name": "x", "source": "rtsp://x"}, vcsrf)
check("viewer cannot add camera (403)", s == 403)
s, j = api("users.php")
check("viewer cannot list users (403)", s == 403)
s, data = api("snapshot.php", "POST", {"name": "testcam"}, vcsrf, raw=True)
check("viewer can snapshot", s == 200, s)
s, j = api("snapshots.php", "DELETE", {"all": True}, vcsrf)
check("viewer cannot purge (403)", s == 403)
api("logout.php", "POST")
s, j = api("login.php", "POST", {"username": "admin", "password": "adminpass1"})
api("cameras.php", "DELETE", {"name": "testcam"}, j.get("csrf"))

print("== corrupt streams.json surfaces an error ==")
sf = pathlib.Path("/tmp/camview-test/streams.json")
orig = sf.read_text()
sf.write_text("[{broken")
s, j = api("cameras.php")
check("corrupt streams.json -> 500 with message", s == 500 and "not valid JSON" in j.get("error", ""), (s, j))
sf.write_text(orig)
s, j = api("cameras.php")
check("recovers after restore", s == 200, s)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
