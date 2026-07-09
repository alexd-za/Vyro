#!/usr/bin/env python3
"""webui.py — local web UI for the Clip Factory (stdlib only, one file).

    python3 tools/webui.py [--port 8787]     serve http://127.0.0.1:8787
    python3 tools/webui.py --check           run the built-in self-test

What it does
  * drag-and-drop videos -> inbox/ -> auto `./clip ingest`
  * campaign board: work/* campaigns, unsorted staged videos, finished clips
  * inline clip gallery (Range-aware /media/ so <video> can seek)
  * assign staged videos to campaigns (creates the campaign if missing)
  * kick off `./clip produce` in background jobs with live tail output
  * memory feed (`./clip mem recall`) and a new-campaign brief form

What it deliberately does NOT do
  * it never binds anything but 127.0.0.1
  * it never serves a file outside the repo (and only out/, work/, inbox/)
  * it never runs `./clip publish` and never passes --send anywhere —
    posting to a live account stays in the terminal, per AGENTS.md.
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}   # same set as ingest.py
MEDIA_EXT = VIDEO_EXT | {".png", ".jpg", ".jpeg", ".webp", ".md", ".txt", ".json"}
MEDIA_DIRS = {"out", "work", "inbox"}          # /media/ may only serve from these
MAX_UPLOAD = 4 * 1024 ** 3                     # 4 GB
MAX_FIELD = 1 << 20                            # non-file multipart field cap
MAX_JSON = 1 << 20                             # JSON POST body cap
MAX_HASHTAGS = 4
GRADES = ("vibrant", "moody", "none")
REFRAMES = ("crop", "blur")

CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".webm": "video/webm", ".avi": "video/x-msvideo", ".m4v": "video/x-m4v",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".json": "application/json",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def slugify(name):
    """Same normalization as ./clip's c_new_campaign."""
    s = re.sub(r"[^A-Za-z0-9._-]", "", str(name).strip().replace(" ", "-"))
    return s.strip("._-")


def fmt_mtime(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- background jobs
class Jobs:
    """Background subprocess runner with a rolling output tail per job."""

    KEEP = 20        # most-recent jobs to report
    TAIL = 4000      # chars of output kept per job

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}
        self._seq = 0

    def spawn(self, label, argv, cwd):
        with self._lock:
            self._seq += 1
            jid = f"job-{self._seq}"
            self._jobs[jid] = {"id": jid, "label": label, "status": "running",
                               "started": time.time(), "finished": None,
                               "rc": None, "tail": ""}
        threading.Thread(target=self._run, args=(jid, argv, cwd), daemon=True).start()
        return jid

    def _append(self, jid, text):
        with self._lock:
            j = self._jobs[jid]
            j["tail"] = (j["tail"] + text)[-self.TAIL:]

    def _run(self, jid, argv, cwd):
        rc = -1
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace")
            for line in proc.stdout:
                self._append(jid, line)
            rc = proc.wait()
        except Exception as e:  # noqa: BLE001 — a job must never kill the server
            self._append(jid, f"\n[webui] job failed to run: {e}\n")
        with self._lock:
            j = self._jobs[jid]
            j["rc"] = rc
            j["status"] = "done" if rc == 0 else "error"
            j["finished"] = time.time()

    def list(self):
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: -j["started"])[:self.KEEP]
            out = []
            for j in jobs:
                end = j["finished"] or time.time()
                out.append({**j, "elapsed": int(end - j["started"])})
            return out


# ---------------------------------------------------------------- pipeline API
class Api:
    """Everything the routes can do, with no HTTP in it. One instance per server."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.jobs = Jobs()
        self._manifest_lock = threading.Lock()

    # ---- safety primitives ----
    def safe_path(self, rel):
        """Repo-relative path -> absolute Path, rejecting anything outside the repo."""
        rel = str(rel)
        if not rel or rel.startswith(("/", "~")) or "\\" in rel or "\x00" in rel:
            raise ApiError(400, "bad path")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ApiError(400, "path escapes the repo") from None
        return p

    def media_path(self, rel):
        """Like safe_path but restricted to media dirs + media extensions."""
        p = self.safe_path(rel)
        relparts = p.relative_to(self.root).parts
        if not relparts or relparts[0] not in MEDIA_DIRS:
            raise ApiError(404, "not served")
        if p.suffix.lower() not in MEDIA_EXT:
            raise ApiError(404, "not a media file")
        if not p.is_file():
            raise ApiError(404, "not found")
        return p

    def upload_dest(self, filename):
        """Sanitized inbox destination for an uploaded file; video extensions only."""
        name = Path(str(filename).replace("\\", "/")).name
        name = re.sub(r"[^A-Za-z0-9._ -]", "", name).strip().replace(" ", "_")
        if not name or Path(name).suffix.lower() not in VIDEO_EXT:
            raise ApiError(400,
                           f"only video files allowed ({' '.join(sorted(VIDEO_EXT))})")
        dest = self.root / "inbox" / name
        n = 1
        while dest.exists():
            dest = self.root / "inbox" / f"{Path(name).stem}-{n}{Path(name).suffix}"
            n += 1
        return dest

    def run_clip(self, args, timeout=120):
        """Run ./clip <args> synchronously (list-form, cwd=repo). Publishing is blocked."""
        args = [str(a) for a in args]
        if any(a == "--send" for a in args) or (args and args[0] == "publish"):
            raise ApiError(403, "publishing is not available from the web UI — use the terminal")
        proc = subprocess.run([str(self.root / "clip"), *args], cwd=str(self.root),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", timeout=timeout)
        return proc.returncode, proc.stdout

    def spawn_clip_job(self, label, args):
        args = [str(a) for a in args]
        if any(a == "--send" for a in args) or (args and args[0] == "publish"):
            raise ApiError(403, "publishing is not available from the web UI — use the terminal")
        return self.jobs.spawn(label, [str(self.root / "clip"), *args], self.root)

    # ---- reads ----
    def mode(self):
        env = self.root / ".env"
        mode = "offline"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith("MODE="):
                    mode = line.split("=", 1)[1].strip().strip("\"'") or "offline"
        return "online" if mode in ("online", "publish") else "offline"

    def _load_manifest(self):
        mf = self.root / "knowledge" / "inbox-manifest.json"
        if not mf.is_file():
            return {}
        try:
            return json.loads(mf.read_text())
        except json.JSONDecodeError:
            return {}

    def _clips_for(self, campaign):
        d = self.root / "out" / campaign
        clips = []
        if not d.is_dir():
            return clips
        for f in sorted(d.glob("*.mp4")):
            st = f.stat()
            rel = f"out/{campaign}/{f.name}"
            sheet = f.with_name(f.stem + "_sheet.png")
            content = f.with_name(f.stem + ".content.md")
            clips.append({
                "name": f.name, "rel": rel, "size": st.st_size,
                "mtime": fmt_mtime(st.st_mtime),
                "final": f.stem.endswith("_final"),
                "sheet": f"out/{campaign}/{sheet.name}" if sheet.is_file() else None,
                "content": f"out/{campaign}/{content.name}" if content.is_file() else None,
            })
        return clips

    def state(self):
        work, out = self.root / "work", self.root / "out"
        names = set()
        for base in (work, out):
            if base.is_dir():
                names |= {d.name for d in base.iterdir()
                          if d.is_dir() and not d.name.startswith("_")}
        campaigns = []
        for name in sorted(names):
            sources = []
            wd = work / name
            if wd.is_dir():
                for f in sorted(wd.iterdir()):
                    if f.is_file() and f.suffix.lower() in VIDEO_EXT:
                        st = f.stat()
                        sources.append({"name": f.name, "rel": f"work/{name}/{f.name}",
                                        "size": st.st_size, "mtime": fmt_mtime(st.st_mtime)})
            campaigns.append({"name": name,
                              "has_brief": (self.root / "briefs" / f"{name}.json").is_file(),
                              "sources": sources,
                              "clips": self._clips_for(name)})
        unsorted = []
        for vid, e in self._load_manifest().items():
            if e.get("category") is not None:
                continue
            staged = e.get("staged", "")
            try:  # keep the entry even if the staged copy moved/vanished
                rel = str(self.safe_path(staged).relative_to(self.root)) if staged else ""
            except ApiError:
                rel = ""
            unsorted.append({"id": vid, "name": e.get("name", "?"),
                             "duration": e.get("duration"), "size": e.get("size"),
                             "staged": rel})
        return {"mode": self.mode(), "campaigns": campaigns, "unsorted": unsorted,
                "generated": datetime.now().isoformat(timespec="seconds")}

    def memory(self):
        try:
            rc, out = self.run_clip(["mem", "recall"], timeout=30)
        except (ApiError, OSError, subprocess.TimeoutExpired) as e:
            return {"lines": [f"(memory unavailable: {e})"]}
        lines = [l for l in out.splitlines() if l.strip()]
        return {"lines": lines[-15:] or ["(memory is empty)"]}

    # ---- actions ----
    def ingest(self):
        # sort auto runs right after ingest so drops land in named campaigns
        return self.jobs.spawn("ingest + auto-sort",
                               ["bash", "-c", "./clip ingest && ./clip sort auto --yes"],
                               self.root)

    def assign(self, vid, campaign):
        slug = slugify(campaign)
        if not slug:
            raise ApiError(400, "campaign name required")
        with self._manifest_lock:
            man = self._load_manifest()
            entry = man.get(str(vid))
            if not entry:
                raise ApiError(404, f"no staged video with id {vid!r}")
            if not (self.root / "work" / slug).is_dir():
                rc, out = self.run_clip(["new-campaign", slug])
                if rc != 0:
                    raise ApiError(500, f"new-campaign failed: {out[-300:]}")
            staged = self.safe_path(entry.get("staged", ""))
            if not staged.is_file():
                raise ApiError(409, f"staged file missing: {entry.get('staged')}")
            dest = self.root / "work" / slug / staged.name
            if dest.exists():
                raise ApiError(409, f"work/{slug}/{staged.name} already exists")
            staged.replace(dest)
            entry["category"] = slug
            entry["staged"] = f"work/{slug}/{staged.name}"
            entry["assigned"] = datetime.now().isoformat(timespec="seconds")
            mf = self.root / "knowledge" / "inbox-manifest.json"
            mf.parent.mkdir(parents=True, exist_ok=True)
            mf.write_text(json.dumps(man, indent=2))
        try:
            self.run_clip(["mem", "log", f"webui: assigned {staged.name} -> {slug}",
                           "--tags", "stage=categorize"], timeout=20)
        except (ApiError, OSError, subprocess.TimeoutExpired):
            pass
        return {"campaign": slug, "moved": entry["staged"]}

    def produce(self, clip_rel, hook="", grade="vibrant", reframe="crop"):
        p = self.safe_path(clip_rel)
        rel = p.relative_to(self.root)
        if len(rel.parts) < 3 or rel.parts[0] != "out" or p.suffix.lower() != ".mp4":
            raise ApiError(400, "clip must be an .mp4 under out/<campaign>/")
        if not p.is_file():
            raise ApiError(404, f"not found: {rel}")
        if grade not in GRADES:
            raise ApiError(400, f"grade must be one of {GRADES}")
        if reframe not in REFRAMES:
            raise ApiError(400, f"reframe must be one of {REFRAMES}")
        hook = str(hook or "").strip()[:120]
        args = ["produce", str(rel), "--grade", grade, "--reframe", reframe]
        brief = self.root / "briefs" / f"{rel.parts[1]}.json"
        if brief.is_file():
            args += ["--brief", f"briefs/{brief.name}"]
        if hook:
            args += ["--hook", hook]
        jid = self.spawn_clip_job(f"produce {p.name} ({grade}/{reframe})", args)
        return {"job": jid, "command": "./clip " + " ".join(args)}

    def new_campaign(self, name, hashtags, disclose_ad, banned_phrases, notes=""):
        slug = slugify(name)
        if not slug:
            raise ApiError(400, "campaign name required")
        tags = []
        for t in hashtags or []:
            t = str(t).strip()
            if not t:
                continue
            t = "#" + re.sub(r"[^A-Za-z0-9_]", "", t.lstrip("#"))
            if len(t) > 1 and t not in tags:
                tags.append(t)
        if len(tags) > MAX_HASHTAGS:
            raise ApiError(400, f"max {MAX_HASHTAGS} required hashtags (got {len(tags)})")
        banned = [str(b).strip() for b in (banned_phrases or []) if str(b).strip()]
        brief = self.root / "briefs" / f"{slug}.json"
        if brief.exists():
            raise ApiError(409, f"briefs/{slug}.json already exists")
        (self.root / "work" / slug).mkdir(parents=True, exist_ok=True)
        (self.root / "out" / slug).mkdir(parents=True, exist_ok=True)
        brief.parent.mkdir(parents=True, exist_ok=True)
        # same schema as ./clip's c_new_campaign
        brief.write_text(json.dumps({
            "campaign": slug,
            "caption_template": "WRITE A SCROLL-STOPPING FIRST LINE HERE",
            "required_hashtags": tags or ["#fyp"],
            "banned_phrases": banned,
            "min_seconds": 0,
            "max_seconds": 0,
            "brand_color": "#FF6A2C",
            "highlight_words": [],
            "ai_generated": True,
            "is_branded": False,
            "disclosure": "#ad" if disclose_ad else "",
            "video_url": "",
            "platforms": ["tiktok", "reels", "shorts"],
            "notes": str(notes or "Created from the web UI — paste the campaign's rules here."),
        }, indent=2) + "\n")
        return {"campaign": slug, "brief": f"briefs/{slug}.json"}


# ---------------------------------------------------------------- multipart (streaming)
class _FileSink:
    """Writes one uploaded part to <dest>.part, renamed to <dest> on clean close."""

    def __init__(self, dest):
        self.dest = dest
        self.tmp = dest.with_name(dest.name + ".part")
        self.fh = self.tmp.open("wb")

    def write(self, data):
        self.fh.write(data)

    def close(self, ok=True):
        self.fh.close()
        if ok:
            self.tmp.replace(self.dest)
        else:
            self.tmp.unlink(missing_ok=True)


def parse_multipart(rfile, content_type, length, open_sink):
    """Stream-parse a multipart/form-data body of `length` bytes.

    open_sink(field_name, filename) -> _FileSink for file parts (may raise ApiError).
    Returns (fields: dict, files: [Path]). Never loads a file part into memory.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not m:
        raise ApiError(400, "malformed multipart request")
    delim = b"--" + m.group(1).encode()
    marker = b"\r\n" + delim
    state = {"buf": b"", "left": length}

    def fill():
        if state["left"] <= 0:
            return False
        data = rfile.read(min(256 * 1024, state["left"]))
        if not data:
            state["left"] = 0
            return False
        state["left"] -= len(data)
        state["buf"] += data
        return True

    while delim not in state["buf"]:
        if not fill():
            raise ApiError(400, "truncated multipart body")
    state["buf"] = state["buf"].split(delim, 1)[1]

    fields, files = {}, []
    while True:
        while len(state["buf"]) < 2 and fill():
            pass
        if state["buf"][:2] == b"--" or state["left"] <= 0 and not state["buf"].strip():
            break
        while b"\r\n\r\n" not in state["buf"]:
            if not fill():
                raise ApiError(400, "truncated multipart headers")
        raw, state["buf"] = state["buf"].split(b"\r\n\r\n", 1)
        head = raw.decode("utf-8", "replace")
        name = (re.search(r'name="([^"]*)"', head) or [None, ""])[1]
        fm = re.search(r'filename="([^"]*)"', head)
        filename = fm.group(1) if fm else None

        sink, value, ok = None, b"", False
        if filename:
            sink = open_sink(name, filename)
        try:
            while marker not in state["buf"]:
                if len(state["buf"]) > len(marker):
                    safe, state["buf"] = state["buf"][:-len(marker)], state["buf"][-len(marker):]
                    if sink:
                        sink.write(safe)
                    else:
                        value += safe
                        if len(value) > MAX_FIELD:
                            raise ApiError(413, "form field too large")
                if not fill():
                    raise ApiError(400, "unterminated multipart part")
            data, state["buf"] = state["buf"].split(marker, 1)
            if sink:
                sink.write(data)
            else:
                value += data
            ok = True
        finally:
            if sink:
                sink.close(ok)
        if filename:
            files.append(sink.dest)
        elif name:
            fields[name] = value.decode("utf-8", "replace")
    return fields, files


# ---------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = "ClipFactoryUI/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def api(self):
        return self.server.api

    def log_message(self, fmt, *args):  # keep the terminal readable
        sys.stderr.write("  ui  %s\n" % (fmt % args))

    # ---- helpers ----
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_JSON:
            raise ApiError(400, "bad request body")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise ApiError(400, "invalid JSON") from None

    def guarded(self, fn):
        try:
            fn()
        except ApiError as e:
            self.send_json({"error": e.message}, e.status)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"internal error: {e}"}, 500)

    # ---- GET ----
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self.guarded(lambda: self.send_json(self.api.state()))
        elif path == "/api/jobs":
            self.guarded(lambda: self.send_json({"jobs": self.api.jobs.list()}))
        elif path == "/api/memory":
            self.guarded(lambda: self.send_json(self.api.memory()))
        elif path.startswith("/media/"):
            self.guarded(lambda: self.serve_media(path[len("/media/"):]))
        else:
            self.send_json({"error": "not found"}, 404)

    def serve_media(self, rel):
        p = self.api.media_path(rel)
        size = p.stat().st_size
        ctype = CONTENT_TYPES.get(p.suffix.lower(), "application/octet-stream")
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with p.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---- POST ----
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        routes = {"/api/upload": self.post_upload, "/api/assign": self.post_assign,
                  "/api/produce": self.post_produce, "/api/campaign": self.post_campaign}
        fn = routes.get(path)
        if fn is None:
            self.send_json({"error": "not found"}, 404)
        else:
            self.guarded(fn)

    def post_upload(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ApiError(411, "Content-Length required")
        if length > MAX_UPLOAD:
            raise ApiError(413, "upload exceeds the 4 GB cap")
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            raise ApiError(400, "expected multipart/form-data")
        (self.api.root / "inbox").mkdir(exist_ok=True)
        _, files = parse_multipart(self.rfile, ctype, length,
                                   lambda name, fn: _FileSink(self.api.upload_dest(fn)))
        if not files:
            raise ApiError(400, "no video file in the upload")
        jid = self.api.ingest()
        self.send_json({"saved": [f.name for f in files], "job": jid})

    def post_assign(self):
        body = self.read_json()
        result = self.api.assign(body.get("id", ""), body.get("campaign", ""))
        self.send_json(result)

    def post_produce(self):
        body = self.read_json()
        result = self.api.produce(body.get("clip", ""), hook=body.get("hook", ""),
                                  grade=body.get("grade", "vibrant"),
                                  reframe=body.get("reframe", "crop"))
        self.send_json(result)

    def post_campaign(self):
        body = self.read_json()
        result = self.api.new_campaign(body.get("name", ""),
                                       body.get("hashtags", []),
                                       bool(body.get("disclose_ad")),
                                       body.get("banned_phrases", []),
                                       notes=body.get("notes", ""))
        self.send_json(result)


# ---------------------------------------------------------------- the page
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clip Factory</title>
<link rel="icon" href="data:,">
<style>
:root{
  --bg:#faf8f5; --panel:#ffffff; --panel2:#f4f1ec; --border:#e7e2da;
  --text:#221d16; --muted:#8d8578; --accent:#FF6A2C; --accent-ink:#c74a12;
  --accent-soft:rgba(255,106,44,.09); --ok:#25904f; --err:#cf4433; --r:12px;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#131110; --panel:#1c1917; --panel2:#242019; --border:#2e2922;
    --text:#ede8e0; --muted:#948b7c; --accent:#FF6A2C; --accent-ink:#ff8a55;
    --accent-soft:rgba(255,106,44,.13); --ok:#4cc47e; --err:#f07862;
  }
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  padding:0 20px 64px;
}
.wrap{max-width:1180px;margin:0 auto}
header{display:flex;align-items:center;gap:14px;padding:26px 2px 20px;flex-wrap:wrap}
.wordmark{font-size:21px;font-weight:750;letter-spacing:-.02em}
.wordmark b{color:var(--accent);font-weight:750}
.badge{
  font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
  padding:4px 11px;border-radius:999px;border:1px solid var(--border);color:var(--muted);
}
.badge.online{color:var(--ok);border-color:currentColor}
.badge.offline{color:var(--accent-ink);border-color:currentColor}
header .hint{margin-left:auto;font-size:12.5px;color:var(--muted)}
main{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:22px}
@media(max-width:940px){main{grid-template-columns:1fr}}
section{margin-bottom:26px}
h2.label{
  font-size:11px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted);margin-bottom:10px;font-variant:all-small-caps;
}
.card{background:var(--panel);border:1px solid var(--border);border-radius:var(--r);padding:16px}
.empty{color:var(--muted);font-size:13.5px;padding:10px 2px}
code{background:var(--panel2);border-radius:5px;padding:1px 6px;font-size:12.5px}

/* drop zone */
.drop{
  border:2px dashed var(--border);border-radius:16px;background:var(--panel);
  padding:44px 24px;text-align:center;transition:border-color .15s,background .15s;
  margin-bottom:26px;
}
.drop.over{border-color:var(--accent);background:var(--accent-soft)}
.drop .glyph{
  width:44px;height:44px;margin:0 auto 12px;border-radius:12px;
  background:var(--accent-soft);color:var(--accent);display:grid;place-items:center;
  font-size:20px;font-weight:700;
}
.drop .t1{font-weight:650;font-size:16px}
.drop .t2{color:var(--muted);font-size:13px;margin:4px 0 14px}
button{
  font:inherit;font-size:13.5px;font-weight:600;color:var(--text);
  background:var(--panel2);border:1px solid var(--border);border-radius:9px;
  padding:7px 14px;cursor:pointer;
}
button:hover{border-color:var(--accent);color:var(--accent-ink)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:hover{filter:brightness(1.06);color:#fff}
button:disabled{opacity:.5;cursor:default}

/* staged / unsorted */
.staged{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:10px}
.staged .who{flex:1;min-width:180px}
.fname{font-weight:650;font-size:14px;word-break:break-all}
.meta{color:var(--muted);font-size:12.5px}
form.assign{display:flex;gap:8px}
input,select,textarea{
  font:inherit;font-size:13.5px;color:var(--text);background:var(--bg);
  border:1px solid var(--border);border-radius:9px;padding:7px 10px;min-width:0;
}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}

/* campaigns */
.camp{margin-bottom:16px}
.camp-head{display:flex;align-items:baseline;gap:10px;margin-bottom:10px}
.camp-head .name{font-size:17px;font-weight:700;letter-spacing:-.01em}
.pill{
  font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
  border:1px solid var(--border);color:var(--muted);border-radius:999px;padding:2px 8px;
}
.pill.final{color:var(--ok);border-color:currentColor}
.pill.brief{color:var(--accent-ink);border-color:currentColor}
ul.sources{list-style:none;margin-bottom:12px}
ul.sources li{display:flex;gap:10px;align-items:baseline;padding:3px 0;flex-wrap:wrap}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.clip{background:var(--panel2);border:1px solid var(--border);border-radius:var(--r);
  padding:10px;display:flex;flex-direction:column;gap:8px}
.clip video{width:100%;aspect-ratio:9/16;max-height:340px;border-radius:8px;background:#000}
.clip-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.clip-head .fname{flex:1;font-size:12.5px}
details{border-top:1px solid var(--border);padding-top:6px}
details summary{cursor:pointer;font-size:12.5px;color:var(--muted);user-select:none}
details summary:hover{color:var(--accent-ink)}
details img{width:100%;border-radius:8px;margin-top:8px}
details pre{
  margin-top:8px;font-size:11.5px;line-height:1.5;white-space:pre-wrap;
  word-break:break-word;max-height:260px;overflow:auto;color:var(--text);
  background:var(--bg);border-radius:8px;padding:10px;
}
form.produce{display:flex;flex-direction:column;gap:7px;border-top:1px solid var(--border);padding-top:9px}
form.produce .row{display:flex;gap:7px}
form.produce select{flex:1}

/* new campaign */
#newcamp form{display:grid;grid-template-columns:1fr 1fr;gap:12px}
#newcamp .full{grid-column:1/-1}
#newcamp label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:var(--muted)}
#newcamp .check{flex-direction:row;align-items:center;gap:8px;color:var(--text)}
#newcamp input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px}
#newcamp .actions{grid-column:1/-1;display:flex;align-items:center;gap:12px}
.fieldnote{font-size:11.5px;color:var(--muted)}
.fieldnote.bad{color:var(--err);font-weight:600}

/* rail */
.job{margin-bottom:10px}
.job-head{display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.dot.running{background:var(--accent);animation:pulse 1.1s ease-in-out infinite}
.dot.done{background:var(--ok)} .dot.error{background:var(--err)}
@keyframes pulse{50%{opacity:.35}}
.job .label2{flex:1;font-size:13px;font-weight:600;word-break:break-all}
.job pre{
  margin-top:6px;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;
  max-height:150px;overflow:auto;background:var(--panel2);border-radius:8px;padding:8px;color:var(--muted);
}
pre.mem{
  font-size:11.5px;line-height:1.6;white-space:pre-wrap;word-break:break-word;
  color:var(--muted);max-height:420px;overflow:auto;
}
#toast{
  position:fixed;left:50%;bottom:24px;transform:translate(-50%,20px);opacity:0;
  background:var(--text);color:var(--bg);font-size:13.5px;font-weight:600;
  padding:10px 18px;border-radius:10px;pointer-events:none;transition:.25s;max-width:80vw;
}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.err{background:var(--err);color:#fff}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="wordmark">Clip<b>Factory</b></div>
    <span id="mode" class="badge">…</span>
    <span class="hint">local · 127.0.0.1 · posting stays in the terminal</span>
  </header>
  <main>
    <div>
      <div id="drop" class="drop">
        <div class="glyph">▼</div>
        <div class="t1">Drop a campaign video to start</div>
        <div class="t2">.mp4 · .mov · .mkv · .webm · .avi · .m4v — saved to <code>inbox/</code>, then ingested automatically</div>
        <button id="pick" type="button">Choose files…</button>
        <input type="file" id="file" multiple accept=".mp4,.mov,.mkv,.webm,.avi,.m4v,video/*" hidden>
      </div>

      <section id="unsorted-sec" hidden>
        <h2 class="label">Staged — needs a campaign</h2>
        <div id="unsorted"></div>
      </section>

      <section>
        <h2 class="label">Campaign board</h2>
        <div id="campaigns"><div class="empty">Loading…</div></div>
      </section>

      <section class="card" id="newcamp">
        <h2 class="label">New campaign</h2>
        <form id="campform">
          <label>Name
            <input name="name" placeholder="e.g. beast-games" required>
          </label>
          <label>Required hashtags <span class="fieldnote">(comma separated, max 4)</span>
            <input name="hashtags" placeholder="#fyp, #brandname">
          </label>
          <label class="full">Banned phrases <span class="fieldnote">(comma separated — wording the brief forbids)</span>
            <input name="banned" placeholder="guaranteed results, cures">
          </label>
          <label class="check full"><input type="checkbox" name="ad"> Requires <code>#ad</code> disclosure</label>
          <div class="actions">
            <button class="primary" type="submit">Create brief</button>
            <span class="fieldnote" id="campnote">writes briefs/&lt;name&gt;.json + work/ and out/ folders</span>
          </div>
        </form>
      </section>
    </div>

    <aside>
      <section>
        <h2 class="label">Jobs</h2>
        <div id="jobs"><div class="empty">Nothing running — produce a clip or drop a video.</div></div>
      </section>
      <section class="card">
        <h2 class="label">Memory</h2>
        <pre class="mem" id="memory">…</pre>
      </section>
    </aside>
  </main>
</div>
<div id="toast"></div>

<script>
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const mediaURL = rel => "/media/" + String(rel).split("/").map(encodeURIComponent).join("/");
const fmtSize = n => n == null ? "" :
  n >= 1<<30 ? (n/(1<<30)).toFixed(1)+" GB" :
  n >= 1<<20 ? (n/(1<<20)).toFixed(1)+" MB" :
  n >= 1024  ? Math.round(n/1024)+" KB" : n+" B";
const fmtDur = s => s == null ? "" : Math.round(s)+"s";

let toastT;
function toast(msg, isErr){
  const t = $("#toast");
  t.textContent = msg; t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastT); toastT = setTimeout(() => t.className = "", 3600);
}
async function api(path, opts){
  const r = await fetch(path, opts);
  let j = {}; try { j = await r.json(); } catch(e){}
  if(!r.ok) throw new Error(j.error || (r.status + " " + r.statusText));
  return j;
}

/* ---------- upload ---------- */
const drop = $("#drop");
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.add("over"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => {
  e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => upload(e.dataTransfer.files));
$("#pick").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });

async function upload(files){
  if(!files || !files.length) return;
  const fd = new FormData();
  let n = 0;
  for(const f of files){
    if(!/\.(mp4|mov|mkv|webm|avi|m4v)$/i.test(f.name)){ toast("skipped " + f.name + " — not a video", true); continue; }
    if(f.size > 4*1024*1024*1024){ toast("skipped " + f.name + " — over the 4 GB cap", true); continue; }
    fd.append("video", f, f.name); n++;
  }
  if(!n) return;
  drop.querySelector(".t1").textContent = "Uploading…";
  try{
    const r = await api("/api/upload", { method: "POST", body: fd });
    toast("saved " + r.saved.join(", ") + " — ingesting");
    refreshJobs(); setTimeout(refreshState, 1200);
  }catch(e){ toast(e.message, true); }
  drop.querySelector(".t1").textContent = "Drop a campaign video to start";
}

/* ---------- state ---------- */
function suggestSlug(name){
  return name.replace(/\.[^.]+$/, "").toLowerCase()
             .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
}
function unsortedRow(u){
  return `<div class="card staged">
    <div class="who"><div class="fname">${esc(u.name)}</div>
      <div class="meta">${fmtDur(u.duration)}${u.duration!=null?" · ":""}${fmtSize(u.size)} · id ${esc(u.id)}</div></div>
    <form class="assign" data-id="${esc(u.id)}">
      <input name="campaign" placeholder="campaign name" value="${esc(suggestSlug(u.name))}" required>
      <button class="primary">Assign</button>
    </form></div>`;
}
function clipCard(k){
  const tag = k.final ? '<span class="pill final">final</span>' : '<span class="pill">cut</span>';
  let extra = "";
  if(k.sheet) extra += `<details><summary>Contact sheet</summary>
      <img loading="lazy" src="${mediaURL(k.sheet)}" alt="contact sheet"></details>`;
  if(k.content) extra += `<details class="contentmd" data-src="${mediaURL(k.content)}">
      <summary>Content sheet</summary><pre>loading…</pre></details>`;
  const prod = k.final ? "" : `
    <form class="produce" data-rel="${esc(k.rel)}">
      <input name="hook" placeholder="Hook title (on-screen, first 2s)" maxlength="120">
      <div class="row">
        <select name="grade"><option>vibrant</option><option>moody</option><option>none</option></select>
        <select name="reframe"><option>crop</option><option>blur</option></select>
      </div>
      <button class="primary">Produce 9:16</button>
    </form>`;
  return `<div class="clip">
    <video controls preload="metadata" src="${mediaURL(k.rel)}"></video>
    <div class="clip-head"><span class="fname">${esc(k.name)}</span>${tag}
      <span class="meta">${fmtSize(k.size)}</span></div>
    ${extra}${prod}</div>`;
}
function campaignCard(c){
  const srcs = c.sources.length
    ? c.sources.map(s => `<li><span class="fname">${esc(s.name)}</span>
        <span class="meta">${fmtSize(s.size)} · ${esc(s.mtime)}</span></li>`).join("")
    : `<li class="empty">no source video yet — assign one from staged, or drop one above</li>`;
  const clips = c.clips.length ? c.clips.map(clipCard).join("")
    : `<div class="empty">No clips yet — cut moments with <code>./clip cut</code>, they show up here.</div>`;
  return `<div class="card camp">
    <div class="camp-head"><span class="name">${esc(c.name)}</span>
      ${c.has_brief ? '<span class="pill brief">brief</span>' : '<span class="pill">no brief</span>'}
      <span class="meta">${c.clips.length} clip${c.clips.length===1?"":"s"}</span></div>
    <ul class="sources">${srcs}</ul>
    <div class="clips">${clips}</div></div>`;
}
async function refreshState(){
  try{
    const s = await api("/api/state");
    const badge = $("#mode");
    badge.textContent = s.mode; badge.className = "badge " + s.mode;
    $("#unsorted-sec").hidden = !s.unsorted.length;
    $("#unsorted").innerHTML = s.unsorted.map(unsortedRow).join("");
    $("#campaigns").innerHTML = s.campaigns.length
      ? s.campaigns.map(campaignCard).join("")
      : `<div class="empty">No campaigns yet — drop a video to start, or create one below.</div>`;
  }catch(e){ toast("state: " + e.message, true); }
}

/* delegated: assign + produce + lazy content sheets */
document.addEventListener("submit", async e => {
  const f = e.target;
  if(f.matches("form.assign")){
    e.preventDefault();
    const btn = f.querySelector("button"); btn.disabled = true;
    try{
      const r = await api("/api/assign", { method:"POST",
        headers:{ "Content-Type":"application/json" },
        body: JSON.stringify({ id: f.dataset.id, campaign: f.campaign.value }) });
      toast("assigned to " + r.campaign); refreshState();
    }catch(err){ toast(err.message, true); btn.disabled = false; }
  }
  if(f.matches("form.produce")){
    e.preventDefault();
    const btn = f.querySelector("button"); btn.disabled = true;
    try{
      const r = await api("/api/produce", { method:"POST",
        headers:{ "Content-Type":"application/json" },
        body: JSON.stringify({ clip: f.dataset.rel, hook: f.hook.value,
                               grade: f.grade.value, reframe: f.reframe.value }) });
      toast("producing — watch the jobs panel"); refreshJobs();
    }catch(err){ toast(err.message, true); }
    btn.disabled = false;
  }
});
document.addEventListener("toggle", async e => {
  const d = e.target;
  if(d.matches && d.matches("details.contentmd") && d.open && !d.dataset.loaded){
    d.dataset.loaded = "1";
    try{
      const r = await fetch(d.dataset.src);
      d.querySelector("pre").textContent = r.ok ? await r.text() : "could not load";
    }catch(err){ d.querySelector("pre").textContent = "could not load"; }
  }
}, true);

/* ---------- new campaign ---------- */
$("#campform").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target;
  const tags = f.hashtags.value.split(",").map(t => t.trim()).filter(Boolean);
  const note = $("#campnote");
  if(tags.length > 4){
    note.textContent = "max 4 required hashtags — you have " + tags.length;
    note.className = "fieldnote bad"; return;
  }
  note.className = "fieldnote";
  note.textContent = "writes briefs/<name>.json + work/ and out/ folders";
  try{
    const r = await api("/api/campaign", { method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({
        name: f.name.value, hashtags: tags, disclose_ad: f.ad.checked,
        banned_phrases: f.banned.value.split(",").map(t => t.trim()).filter(Boolean) }) });
    toast("created " + r.brief + " — fill in the campaign's rules");
    f.reset(); refreshState();
  }catch(err){ toast(err.message, true); }
});

/* ---------- jobs + memory polling ---------- */
let prevStatus = {};
async function refreshJobs(){
  try{
    const { jobs } = await api("/api/jobs");
    let stateStale = false;
    for(const j of jobs){
      if(prevStatus[j.id] === "running" && j.status !== "running") stateStale = true;
      prevStatus[j.id] = j.status;
    }
    $("#jobs").innerHTML = jobs.length ? jobs.map(j => `
      <div class="card job">
        <div class="job-head"><span class="dot ${esc(j.status)}"></span>
          <span class="label2">${esc(j.label)}</span>
          <span class="meta">${j.elapsed}s</span></div>
        ${j.tail ? `<pre>${esc(j.tail.split("\n").slice(-12).join("\n").trim())}</pre>` : ""}
      </div>`).join("")
      : `<div class="empty">Nothing running — produce a clip or drop a video.</div>`;
    if(stateStale){ refreshState(); refreshMemory(); }
  }catch(e){ /* server briefly busy — next poll wins */ }
}
async function refreshMemory(){
  try{
    const m = await api("/api/memory");
    $("#memory").textContent = m.lines.join("\n");
  }catch(e){ $("#memory").textContent = "(memory unavailable)"; }
}
refreshState(); refreshJobs(); refreshMemory();
setInterval(refreshJobs, 2000);
setInterval(refreshState, 15000);
setInterval(refreshMemory, 30000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- self-test
def self_check():
    import http.client
    import tempfile
    import urllib.error
    import urllib.request

    root = Path(tempfile.mkdtemp(prefix="clipui-check-"))
    for d in ("work", "out", "briefs", "inbox", "knowledge"):
        (root / d).mkdir()
    api = Api(root)

    # 1. path traversal is rejected
    for bad in ("../etc/passwd", "/etc/passwd", "out/../../evil", "~root/x", "a\\..\\b"):
        try:
            api.safe_path(bad)
            raise AssertionError(f"traversal accepted: {bad!r}")
        except ApiError:
            pass
    assert api.safe_path("out/c/a.mp4") == root / "out" / "c" / "a.mp4"

    # 2. media serving is restricted to media dirs + media extensions
    (root / "briefs" / "x.json").write_text("{}")
    for bad in ("briefs/x.json", "clip", ".env"):
        try:
            api.media_path(bad)
            raise AssertionError(f"media served outside media dirs: {bad!r}")
        except ApiError:
            pass

    # 3. upload extension gate
    for bad in ("evil.exe", "note.txt", "x.mp4.sh", ""):
        try:
            api.upload_dest(bad)
            raise AssertionError(f"non-video upload accepted: {bad!r}")
        except ApiError:
            pass
    assert api.upload_dest("My Clip.MP4").name == "My_Clip.MP4"

    # 4. the 4-hashtag cap
    try:
        api.new_campaign("toomany", ["#a", "#b", "#c", "#d", "#e"], False, [])
        raise AssertionError("hashtag cap not enforced")
    except ApiError as e:
        assert e.status == 400, e.status
    r = api.new_campaign("checkcamp", ["#fyp", "brand"], True, ["guaranteed"])
    brief = json.loads((root / "briefs" / "checkcamp.json").read_text())
    assert brief["campaign"] == "checkcamp"
    assert brief["required_hashtags"] == ["#fyp", "#brand"]
    assert brief["disclosure"] == "#ad" and brief["banned_phrases"] == ["guaranteed"]
    assert (root / "work" / "checkcamp").is_dir() and (root / "out" / "checkcamp").is_dir()
    assert r["brief"] == "briefs/checkcamp.json"

    # 5. publish / --send can never leave this process
    for argv in (["publish", "prepare"], ["produce", "x", "--send"]):
        try:
            api.run_clip(argv)
            raise AssertionError(f"publish path not blocked: {argv}")
        except ApiError as e:
            assert e.status == 403

    # 6. produce validation
    (root / "out" / "checkcamp").mkdir(exist_ok=True)
    clip = root / "out" / "checkcamp" / "a.mp4"
    clip.write_bytes(b"0123456789")
    for kw in ({"grade": "sepia"}, {"reframe": "zoom"}):
        try:
            api.produce("out/checkcamp/a.mp4", **kw)
            raise AssertionError(f"bad produce args accepted: {kw}")
        except ApiError:
            pass
    try:
        api.produce("briefs/checkcamp.json")
        raise AssertionError("produce accepted a non-out path")
    except ApiError:
        pass

    # 7. live routes against the temp root (ephemeral port, 127.0.0.1)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.api = api
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/") as resp:
            assert resp.status == 200 and b"Clip Factory" in resp.read()
        with urllib.request.urlopen(base + "/api/state") as resp:
            st = json.loads(resp.read())
            assert st["mode"] == "offline"
            assert any(c["name"] == "checkcamp" for c in st["campaigns"])

        # Range request -> 206 partial content
        req = urllib.request.Request(base + "/media/out/checkcamp/a.mp4",
                                     headers={"Range": "bytes=2-5"})
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 206, resp.status
            assert resp.read() == b"2345"
            assert resp.headers["Content-Range"] == "bytes 2-5/10"

        # raw-path traversal over HTTP (urllib would normalize it; go raw)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/media/../../../../etc/passwd", skip_accept_encoding=True)
        conn.endheaders()
        assert conn.getresponse().status in (400, 404)
        conn.close()

        # hashtag cap over HTTP
        body = json.dumps({"name": "z", "hashtags": ["#1", "#2", "#3", "#4", "#5"]}).encode()
        req = urllib.request.Request(base + "/api/campaign", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("hashtag cap not enforced over HTTP")
        except urllib.error.HTTPError as e:
            assert e.code == 400

        # multipart upload of a fake video + extension gate over HTTP
        boundary = "checkboundary42"
        part = (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="video"; filename="tiny.mp4"\r\n'
                f"Content-Type: video/mp4\r\n\r\n").encode() + b"FAKEMP4DATA" + \
               f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(base + "/api/upload", data=part,
                                     headers={"Content-Type":
                                              f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req) as resp:
            up = json.loads(resp.read())
            assert up["saved"] == ["tiny.mp4"]
        assert (root / "inbox" / "tiny.mp4").read_bytes() == b"FAKEMP4DATA"
    finally:
        srv.shutdown()

    print("CHECK OK")
    return 0


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Local web UI for the Clip Factory.")
    ap.add_argument("--port", type=int, default=8787, help="port on 127.0.0.1 (default 8787)")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]),
                    help=argparse.SUPPRESS)
    ap.add_argument("--check", action="store_true", help="run the self-test and exit")
    args = ap.parse_args()
    if args.check:
        sys.exit(self_check())
    api = Api(args.root)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.api = api
    print(f"Clip Factory UI  →  http://127.0.0.1:{args.port}")
    print(f"  root: {api.root}")
    print(f"  mode: {api.mode()}  ·  publishing is disabled here (terminal only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
