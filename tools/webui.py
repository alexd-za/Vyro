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
  * sectioned app (Studio / Campaigns / Jobs / Memory / Settings, hash-routed)
  * settings tab writes allowlisted keys to ROOT/.env (gitignored); the API
    never returns a stored secret to the browser — only set/last4

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
MODES = ("offline", "online")

# settings — the ONLY keys /api/settings may ever write to .env
SETTINGS_KEYS = frozenset({
    "OPERATOR_NAME", "MODE", "UPLOADPOST_API_KEY", "SCHEDULER_BASE_URL",
    "SCHEDULER_API_KEY", "TIKTOK_CLIENT_KEY", "TIKTOK_ACCESS_TOKEN",
    "COMPOSIO_API_KEY",
})
MAX_SETTING = 500                              # max chars for one settings value
SETTINGS_BACKENDS = {                          # backend -> the secret that means "connected"
    "uploadpost": "UPLOADPOST_API_KEY",
    "scheduler": "SCHEDULER_API_KEY",
    "tiktok": "TIKTOK_ACCESS_TOKEN",
    "composio": "COMPOSIO_API_KEY",
}

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
        self._env_lock = threading.Lock()

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

    def _env_values(self):
        """Parse ROOT/.env into a dict (quotes stripped)."""
        vals = {}
        f = self.root / ".env"
        if f.is_file():
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip("\"'")
        return vals

    def settings(self):
        """Settings snapshot for the UI. A stored secret NEVER leaves this
        process in full — only whether it is set, plus its last 4 chars."""
        env = self._env_values()

        def status(key):
            v = env.get(key, "")
            if v.startswith("your_"):      # untouched .env.example placeholder
                v = ""
            return {"set": bool(v), "last4": v[-4:] if v else ""}

        operator = env.get("OPERATOR_NAME", "")
        if operator.startswith("your_"):
            operator = ""
        return {"operator": operator, "mode": self.mode(),
                "backends": {name: status(key)
                             for name, key in SETTINGS_BACKENDS.items()}}

    def set_setting(self, key, value):
        """Write one allowlisted KEY=value into ROOT/.env (created from
        .env.example if missing). Empty value clears the key. Values are
        stripped, capped, and may not contain newlines (env-injection guard)."""
        key = str(key or "").strip()
        if key not in SETTINGS_KEYS:
            raise ApiError(400, f"unknown setting {key!r}")
        value = str(value if value is not None else "").strip()
        if len(value) > MAX_SETTING:
            raise ApiError(400, f"value too long (max {MAX_SETTING} chars)")
        if "\n" in value or "\r" in value:
            raise ApiError(400, "newlines are not allowed in a setting")
        if key == "MODE" and value and value not in MODES:
            raise ApiError(400, f"MODE must be one of {MODES}")
        envf = self.root / ".env"
        with self._env_lock:
            if not envf.is_file():
                example = self.root / ".env.example"
                envf.write_text(example.read_text() if example.is_file() else "")
            out, seen = [], False
            for line in envf.read_text().splitlines():
                if re.match(r"\s*" + re.escape(key) + r"\s*=", line):
                    if seen:
                        continue                       # collapse duplicate lines
                    seen = True
                    if value:
                        out.append(f"{key}={value}")   # update in place
                    # empty value -> drop the line entirely (key cleared)
                else:
                    out.append(line)
            if value and not seen:
                out.append(f"{key}={value}")
            envf.write_text("\n".join(out) + "\n" if out else "")
        return self.settings()

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
        elif path == "/api/settings":
            self.guarded(lambda: self.send_json(self.api.settings()))
        elif path == "/vendor/three.min.js":
            self.guarded(self.serve_vendor_three)
        elif path.startswith("/media/"):
            self.guarded(lambda: self.serve_media(path[len("/media/"):]))
        else:
            self.send_json({"error": "not found"}, 404)

    def serve_vendor_three(self):
        p = self.api.root / "assets" / "vendor" / "three.min.js"
        if not p.is_file():
            raise ApiError(404, "three.min.js not installed")
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

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
                  "/api/produce": self.post_produce, "/api/campaign": self.post_campaign,
                  "/api/settings": self.post_settings}
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

    def post_settings(self):
        body = self.read_json()
        self.send_json(self.api.set_setting(body.get("key", ""), body.get("value", "")))

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
/* ============ editorial cinema — tokens ============
   type scale 12/14/16/20/28/40/64 · spacing scale 4/8/12/16/24/40/64
   one ember accent, hairline dividers, display numerals            */
@font-face{
  font-family:"ClipDisplay";
  src:url("/vendor/font-display.woff2") format("woff2");
  font-weight:100 900;font-display:swap;
}
:root{
  --fs-12:.75rem;--fs-14:.875rem;--fs-16:1rem;--fs-20:1.25rem;
  --fs-28:1.75rem;--fs-40:2.5rem;--fs-64:4rem;
  --sp-4:4px;--sp-8:8px;--sp-12:12px;--sp-16:16px;--sp-24:24px;--sp-40:40px;--sp-64:64px;
  --display:"ClipDisplay",ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --body:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --bg:#131110;--raised:#1B1815;
  --ink:#EDE6DC;--mute:#A69C8E;
  --hair:rgba(237,230,220,.13);--hair-strong:rgba(237,230,220,.30);
  --ember:#FF6A2C;--ember-ink:#FF8A50;--ember-soft:rgba(255,106,44,.12);
  --on-ember:#1F0E05;
  --ok:#7BC98F;--ok-soft:rgba(123,201,143,.10);
  --err:#F0876B;--err-soft:rgba(240,135,107,.12);
  --r:8px;--r-sm:4px;
  --ease:cubic-bezier(.22,.61,.36,1);
  --fast:.16s;--med:.22s;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#F6F2EB;--raised:#FDFBF3;
    --ink:#211B15;--mute:#5F564C;
    --hair:rgba(33,27,21,.16);--hair-strong:rgba(33,27,21,.34);
    --ember-ink:#A63E08;--ember-soft:rgba(255,106,44,.10);
    --ok:#1E7A43;--ok-soft:rgba(30,122,67,.10);
    --err:#B23A24;--err-soft:rgba(178,58,36,.10);
  }
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark light}
body{
  background:var(--bg);color:var(--ink);
  font:var(--fs-14)/1.6 var(--body);
  -webkit-font-smoothing:antialiased;font-kerning:normal;
}
[hidden]{display:none !important}
::selection{background:var(--ember-soft);color:var(--ink)}
a{color:inherit}
code{font:var(--fs-12)/1.5 var(--mono);background:var(--raised);
  border:1px solid var(--hair);border-radius:var(--r-sm);padding:1px 5px}
h1,h2,h3{text-wrap:balance}
.skip{position:absolute;left:-9999px;top:0;z-index:100;background:var(--ember);
  color:var(--on-ember);padding:var(--sp-8) var(--sp-16);border-radius:0 0 var(--r) 0;
  font-weight:700;text-decoration:none}
.skip:focus{left:0}
:where(a,button,input,select,textarea,summary):focus-visible{
  outline:2px solid var(--ember);outline-offset:2px;border-radius:var(--r-sm)}

/* ============ shell: sticky rail + asymmetric main column ============ */
.shell{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}
.rail{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
  padding:var(--sp-24);border-right:1px solid var(--hair)}
.logotype{font-family:var(--display);font-weight:850;font-size:var(--fs-20);
  line-height:1.05;letter-spacing:-.02em;text-transform:uppercase;
  text-decoration:none;margin-bottom:var(--sp-40)}
.logotype .stop{color:var(--ember)}
.rail nav{display:flex;flex-direction:column}
.navlink{display:flex;align-items:center;gap:var(--sp-8);padding:var(--sp-12) 2px;
  font-size:var(--fs-14);font-weight:600;color:var(--mute);text-decoration:none;
  border-bottom:1px solid var(--hair);
  transition:color var(--fast) var(--ease)}
.navlink::before{content:"";width:5px;height:5px;border-radius:50%;flex:none;
  background:transparent;transition:background var(--fast) var(--ease)}
.navlink::after{content:"\2192";margin-left:auto;opacity:0;transform:translateX(-4px);
  transition:opacity var(--fast) var(--ease),transform var(--fast) var(--ease)}
.navlink:hover{color:var(--ink)}
.navlink:hover::after{opacity:.6;transform:none}
.navlink[aria-current]{color:var(--ink)}
.navlink[aria-current]::before{background:var(--ember)}
.count{min-width:18px;height:18px;padding:0 5px;border-radius:999px;
  background:var(--ember);color:var(--on-ember);font-size:var(--fs-12);font-weight:800;
  display:inline-grid;place-items:center;line-height:1}
.rail-foot{margin-top:auto;display:flex;flex-direction:column;gap:var(--sp-8);
  padding-top:var(--sp-24)}
.badge{align-self:flex-start;font-family:var(--mono);font-size:var(--fs-12);
  letter-spacing:.08em;text-transform:uppercase;color:var(--mute);
  border:1px solid var(--hair-strong);border-radius:999px;padding:3px 10px}
.badge.online{color:var(--ok);border-color:currentColor;background:var(--ok-soft)}
.badge.offline{color:var(--mute)}
.railnote{font-size:var(--fs-12);line-height:1.7;color:var(--mute)}

main{min-width:0}
.content{max-width:1020px;padding:0 var(--sp-40) var(--sp-64)}

/* ============ hero — the projection strip (stays dark in both themes) ============ */
.hero{position:relative;height:224px;overflow:hidden;background:#131110;
  border-bottom:1px solid var(--hair)}
#fx{position:absolute;inset:0;width:100%;height:100%;display:block}
.hero-veil{position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(180deg,rgba(19,17,16,.10) 0%,rgba(19,17,16,.55) 100%)}
.hero.fallback #fx{display:none}
.hero.fallback::before{content:"";position:absolute;inset:-40%;filter:blur(44px);
  background:
    radial-gradient(30% 40% at 24% 60%, rgba(255,106,44,.30), transparent 70%),
    radial-gradient(24% 36% at 68% 34%, rgba(255,176,110,.16), transparent 70%);
  animation:emberDrift 26s ease-in-out infinite alternate}
@keyframes emberDrift{to{transform:translate(4%,6%) scale(1.12)}}
.hero-inner{position:relative;z-index:2;height:100%;max-width:1020px;
  padding:0 var(--sp-40);display:flex;flex-direction:column;justify-content:center;
  gap:var(--sp-8)}
.kicker{font:600 var(--fs-12)/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
  color:rgba(237,230,220,.62)}
.headline{font-family:var(--display);font-weight:850;letter-spacing:-.02em;
  font-size:clamp(var(--fs-28),1.35rem + 1.6vw,var(--fs-40));line-height:1.06;
  color:#EDE6DC}
.headline .stop{color:var(--ember)}

/* ============ shared ============ */
.view{padding-top:var(--sp-24)}
.narrow{max-width:760px}
.block{margin-top:var(--sp-40)}
.viewhead{padding-bottom:var(--sp-16);border-bottom:1px solid var(--hair);
  margin-bottom:var(--sp-24)}
.viewhead h2{font-family:var(--display);font-weight:850;font-size:var(--fs-28);
  letter-spacing:-.02em;line-height:1.1}
.viewhead p{color:var(--mute);margin-top:var(--sp-4);max-width:60ch}
.rule-label{font-size:var(--fs-12);font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--mute);
  padding-top:var(--sp-16);border-top:1px solid var(--hair);margin-bottom:var(--sp-12)}
.empty{color:var(--mute);padding:var(--sp-12) 0}
.meta{color:var(--mute);font:var(--fs-12)/1.6 var(--mono);font-variant-numeric:tabular-nums}
.fname{font:600 var(--fs-14)/1.5 var(--mono);word-break:break-all}

button{
  font:600 var(--fs-14)/1 var(--body);color:var(--ink);background:transparent;
  border:1px solid var(--hair-strong);border-radius:var(--r);padding:10px 16px;
  cursor:pointer;min-height:36px;
  transition:border-color var(--fast) var(--ease),color var(--fast) var(--ease),
             background var(--med) var(--ease)}
button:hover{border-color:var(--ember);color:var(--ember-ink)}
button.primary{background:var(--ember);border-color:var(--ember);color:var(--on-ember)}
button.primary:hover{background:#FF7C45;border-color:#FF7C45;color:var(--on-ember)}
button:disabled{opacity:.45;cursor:default}
input,select,textarea{
  font:var(--fs-14)/1.4 var(--body);color:var(--ink);background:var(--raised);
  border:1px solid var(--hair);border-radius:var(--r);padding:9px 12px;min-width:0;
  transition:border-color var(--fast) var(--ease)}
input:hover,select:hover{border-color:var(--hair-strong)}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--ember)}
input:focus-visible,select:focus-visible,textarea:focus-visible{
  outline:2px solid var(--ember);outline-offset:1px}
input::placeholder{color:var(--mute)}
@media(pointer:coarse){
  button{min-height:44px}
  input,select{min-height:44px}
  .navlink{padding-top:var(--sp-12);padding-bottom:var(--sp-12)}
}

/* ============ studio: display numerals on a hairline baseline ============ */
.stats{display:flex;gap:var(--sp-40);padding:var(--sp-24) 0;
  border-bottom:1px solid var(--hair);flex-wrap:wrap}
.stat{display:flex;flex-direction:column;gap:var(--sp-4)}
.stat + .stat{border-left:1px solid var(--hair);padding-left:var(--sp-40)}
@media (max-width:560px){.stats{gap:var(--sp-16)}
  .stat + .stat{padding-left:var(--sp-16)}}
.num{font-family:var(--display);font-weight:850;letter-spacing:-.03em;line-height:1;
  font-size:clamp(var(--fs-40),1.9rem + 2.4vw,var(--fs-64));
  font-variant-numeric:tabular-nums}
.cap{font-size:var(--fs-12);font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;color:var(--mute)}

/* ============ drop zone ============ */
.drop{margin-top:var(--sp-40);border:1px dashed var(--hair-strong);
  border-radius:var(--r);padding:var(--sp-40) var(--sp-24);text-align:center;
  transition:border-color var(--fast) var(--ease),background var(--med) var(--ease)}
.drop.over{border-color:var(--ember);background:var(--ember-soft)}
.drop .t1{font-family:var(--display);font-weight:850;font-size:var(--fs-20);
  letter-spacing:-.01em}
.drop .t2{color:var(--mute);font-size:var(--fs-12);margin:var(--sp-8) 0 var(--sp-16)}

/* ============ staged / unsorted ============ */
.staged{display:flex;align-items:center;gap:var(--sp-16);flex-wrap:wrap;
  padding:var(--sp-12) 0;border-bottom:1px solid var(--hair)}
.staged .who{flex:1;min-width:180px}
form.assign{display:flex;gap:var(--sp-8)}

/* ============ campaign board ============ */
.camp{padding:var(--sp-24) 0;border-bottom:1px solid var(--hair)}
.camp-head{display:flex;align-items:baseline;gap:var(--sp-12);flex-wrap:wrap;
  margin-bottom:var(--sp-12)}
.camp-head .name{font-family:var(--display);font-weight:850;font-size:var(--fs-20);
  letter-spacing:-.01em}
.pill{font:700 var(--fs-12)/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  border:1px solid var(--hair-strong);color:var(--mute);border-radius:999px;
  padding:3px 9px}
.pill.final,.pill.on{color:var(--ok);border-color:currentColor;background:var(--ok-soft)}
.pill.brief{color:var(--ember-ink);border-color:currentColor;background:var(--ember-soft)}
ul.sources{list-style:none;margin-bottom:var(--sp-16)}
ul.sources li{display:flex;gap:var(--sp-12);align-items:baseline;flex-wrap:wrap;
  padding:var(--sp-4) 0}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:var(--sp-16)}
.clip{background:var(--raised);border:1px solid var(--hair);border-radius:var(--r);
  padding:var(--sp-12);display:flex;flex-direction:column;gap:var(--sp-8);
  transition:border-color var(--fast) var(--ease)}
.clip:hover{border-color:var(--hair-strong)}
.clip video{width:100%;aspect-ratio:9/16;max-height:320px;border-radius:var(--r-sm);
  background:#000}
.clip-head{display:flex;align-items:center;gap:var(--sp-8);flex-wrap:wrap}
.clip-head .fname{flex:1;font-size:var(--fs-12)}
details{border-top:1px solid var(--hair);padding-top:var(--sp-8)}
details summary{cursor:pointer;font-size:var(--fs-12);color:var(--mute);
  user-select:none;transition:color var(--fast) var(--ease)}
details summary:hover{color:var(--ember-ink)}
details img{width:100%;border-radius:var(--r-sm);margin-top:var(--sp-8)}
details pre{margin-top:var(--sp-8);font:var(--fs-12)/1.6 var(--mono);
  white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;
  background:var(--bg);border:1px solid var(--hair);border-radius:var(--r-sm);
  padding:var(--sp-12)}
form.produce{display:flex;flex-direction:column;gap:var(--sp-8);
  border-top:1px solid var(--hair);padding-top:var(--sp-8)}
form.produce .row{display:flex;gap:var(--sp-8)}
form.produce select{flex:1}

/* ============ new campaign ============ */
#campform{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-16)}
#campform .full{grid-column:1/-1}
#campform label{display:flex;flex-direction:column;gap:var(--sp-4);
  font-size:var(--fs-12);font-weight:600;color:var(--mute)}
#campform .check{flex-direction:row;align-items:center;gap:var(--sp-8);
  color:var(--ink);font-size:var(--fs-14)}
#campform input[type=checkbox]{accent-color:var(--ember);width:16px;height:16px}
#campform .actions{grid-column:1/-1;display:flex;align-items:center;
  gap:var(--sp-16);flex-wrap:wrap}
.fieldnote{font-size:var(--fs-12);color:var(--mute)}
.fieldnote.bad{color:var(--err);font-weight:600}

/* ============ jobs ============ */
.job{padding:var(--sp-16) 0;border-bottom:1px solid var(--hair)}
.job-head{display:flex;align-items:center;gap:var(--sp-8)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mute);flex:none}
.dot.running{background:var(--ember);animation:pulse 1.2s var(--ease) infinite}
.dot.done{background:var(--ok)} .dot.error{background:var(--err)}
@keyframes pulse{50%{opacity:.35}}
.job .label2{flex:1;font:600 var(--fs-14)/1.5 var(--mono);word-break:break-all}
.job .bar{height:2px;overflow:hidden;background:var(--hair);
  margin-top:var(--sp-12);position:relative}
.job .bar i{position:absolute;inset:0;transform:translateX(-100%);
  background:linear-gradient(90deg,transparent,var(--ember),transparent);
  animation:shimmer 1.6s linear infinite}
@keyframes shimmer{to{transform:translateX(100%)}}
.job pre{margin-top:var(--sp-8);font:var(--fs-12)/1.6 var(--mono);white-space:pre-wrap;
  word-break:break-word;max-height:150px;overflow:auto;background:var(--raised);
  border:1px solid var(--hair);border-radius:var(--r-sm);padding:var(--sp-8);
  color:var(--mute)}

/* ============ memory — the log slate (stays dark in both themes) ============ */
.term{background:#131110;border:1px solid var(--hair-strong);border-radius:var(--r);
  overflow:hidden}
.term .tbar{font:var(--fs-12)/1 var(--mono);letter-spacing:.06em;
  color:rgba(237,230,220,.55);padding:var(--sp-12) var(--sp-16);
  border-bottom:1px solid rgba(237,230,220,.13)}
.term .prompt{color:var(--ember)}
pre.mem{font:var(--fs-12)/1.8 var(--mono);white-space:pre-wrap;word-break:break-word;
  color:#C9BFAF;max-height:420px;overflow:auto;padding:var(--sp-16)}
pre.mem::after{content:"\258C";color:var(--ember);
  animation:blink 1.15s steps(1) infinite}
@keyframes blink{50%{opacity:0}}

/* ============ settings ============ */
.setblock{padding:var(--sp-24) 0;border-bottom:1px solid var(--hair)}
.setblock h3{font-family:var(--display);font-weight:850;font-size:var(--fs-16);
  letter-spacing:-.01em;margin-bottom:2px}
.setdesc{font-size:var(--fs-12);color:var(--mute);margin-bottom:var(--sp-12);
  max-width:60ch}
form.setform{display:flex;flex-direction:column;gap:var(--sp-8)}
form.setform label{font-size:var(--fs-12);font-weight:600;color:var(--mute)}
.setrow{display:flex;gap:var(--sp-8)}
.setrow input{flex:1}
.seg{display:inline-flex;gap:var(--sp-4);padding:var(--sp-4);
  border:1px solid var(--hair);border-radius:var(--r);background:var(--raised)}
.seg button{border-color:transparent;padding:8px 20px;min-height:0}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--bg)}
.seg button[aria-pressed="true"]:hover{background:var(--ink);color:var(--bg);
  border-color:transparent}
.conn{padding:var(--sp-16) 0;border-top:1px solid var(--hair);margin-top:var(--sp-12)}
.conn-head{display:flex;align-items:center;gap:var(--sp-12);margin-bottom:2px}
.conn-name{font-weight:700;font-size:var(--fs-14)}
.setnote{font-size:var(--fs-12);color:var(--mute);padding-top:var(--sp-16)}

/* ============ toast ============ */
#toast{position:fixed;left:50%;bottom:var(--sp-24);transform:translate(-50%,12px);
  opacity:0;z-index:50;background:var(--raised);color:var(--ink);
  border:1px solid var(--hair-strong);font-size:var(--fs-14);font-weight:600;
  padding:var(--sp-12) var(--sp-24);border-radius:var(--r);pointer-events:none;
  transition:opacity var(--med) var(--ease),transform var(--med) var(--ease);
  max-width:80vw}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.err{border-color:var(--err);color:var(--err);background:var(--err-soft)}

/* ============ small screens: rail becomes a top bar ============ */
@media(max-width:880px){
  .shell{display:block}
  .rail{position:sticky;top:0;z-index:30;height:auto;flex-direction:row;
    align-items:center;gap:var(--sp-12);padding:var(--sp-8) var(--sp-16);
    border-right:0;border-bottom:1px solid var(--hair);background:var(--bg)}
  .logotype{font-size:var(--fs-14);margin-bottom:0;white-space:nowrap}
  .logotype br{display:none}
  .rail nav{flex-direction:row;flex:1;overflow-x:auto;
    -webkit-overflow-scrolling:touch}
  .navlink{border-bottom:2px solid transparent;padding:var(--sp-12) var(--sp-8);
    white-space:nowrap}
  .navlink::before,.navlink::after{display:none}
  .navlink[aria-current]{border-bottom-color:var(--ember)}
  .rail-foot{margin:0;padding:0}
  .railnote,#operator{display:none}
  .content{padding:0 var(--sp-16) var(--sp-64)}
  .hero{height:168px}
  .hero-inner{padding:0 var(--sp-16)}
  .stats{gap:var(--sp-24)}
  .stat + .stat{padding-left:var(--sp-24)}
  #campform{grid-template-columns:1fr}
  .staged form.assign{width:100%}
  form.assign input{flex:1}
}

/* ============ reduced motion: calm everything ============ */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation:none !important;transition:none !important}
}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<div class="shell">

<header class="rail">
  <a class="logotype" href="#studio">Clip<br>Factory<span class="stop">.</span></a>
  <nav aria-label="Sections">
    <a class="navlink" id="tab-studio" href="#studio">Studio</a>
    <a class="navlink" id="tab-campaigns" href="#campaigns">Campaigns</a>
    <a class="navlink" id="tab-jobs" href="#jobs">Jobs <span class="count" id="jobsbadge" hidden>0</span></a>
    <a class="navlink" id="tab-memory" href="#memory">Memory</a>
    <a class="navlink" id="tab-settings" href="#settings">Settings</a>
  </nav>
  <div class="rail-foot">
    <span id="mode" class="badge">…</span>
    <span id="operator" class="badge" hidden></span>
    <p class="railnote">Local · 127.0.0.1<br>Publishing stays in the terminal.</p>
  </div>
</header>

<main id="main">
  <div class="hero" id="hero">
    <canvas id="fx" aria-hidden="true"></canvas>
    <div class="hero-veil"></div>
    <div class="hero-inner">
      <p class="kicker">Vyro · short-form pipeline · Clip Factory</p>
      <h1 class="headline">Approved video in.<br>Vertical clips out<span class="stop">.</span></h1>
    </div>
  </div>

  <div class="content">

  <section class="view" id="sec-studio">
    <div class="stats">
      <div class="stat"><span class="num" id="stat-campaigns">0</span><span class="cap">Campaigns</span></div>
      <div class="stat"><span class="num" id="stat-clips">0</span><span class="cap">Clips produced</span></div>
      <div class="stat"><span class="num" id="stat-unsorted">0</span><span class="cap">Awaiting sort</span></div>
    </div>

    <div id="drop" class="drop">
      <p class="t1">Drop a campaign video to start</p>
      <p class="t2">.mp4 · .mov · .mkv · .webm · .avi · .m4v — saved to <code>inbox/</code>, then ingested automatically</p>
      <button id="pick" type="button">Choose files…</button>
      <input type="file" id="file" multiple accept=".mp4,.mov,.mkv,.webm,.avi,.m4v,video/*" hidden aria-label="Choose video files">
    </div>

    <div class="block" id="unsorted-sec" hidden>
      <h2 class="rule-label">Staged — needs a campaign</h2>
      <div id="unsorted"></div>
    </div>
  </section>

  <section class="view" id="sec-campaigns" hidden>
    <header class="viewhead">
      <h2>Campaigns</h2>
      <p>Sources, cuts, and finished 9:16 clips for every accepted brief.</p>
    </header>
    <div id="board"><p class="empty">Loading…</p></div>

    <div class="block" id="newcamp">
      <h2 class="rule-label">New campaign</h2>
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
    </div>
  </section>

  <section class="view narrow" id="sec-jobs" hidden>
    <header class="viewhead">
      <h2>Jobs</h2>
      <p>Ingest, sort, and produce runs with live output.</p>
    </header>
    <div id="joblist"><p class="empty">Nothing running — produce a clip or drop a video.</p></div>
  </section>

  <section class="view narrow" id="sec-memory" hidden>
    <header class="viewhead">
      <h2>Memory</h2>
      <p>What every agent in this repo remembers — the tail of <code>clip mem recall</code>.</p>
    </header>
    <div class="term">
      <div class="tbar"><span class="prompt">$</span> clip mem recall</div>
      <pre class="mem" id="memlog">…</pre>
    </div>
  </section>

  <section class="view narrow" id="sec-settings" hidden>
    <header class="viewhead">
      <h2>Settings</h2>
      <p>Written to <code>.env</code> on this machine only. A saved secret never returns to the browser — only its last 4 characters.</p>
    </header>

    <div class="setblock">
      <h3>Operator</h3>
      <p class="setdesc">Who is running this factory — shown in the rail and used to attribute updates.</p>
      <form class="setform" id="opform">
        <label for="opname">Operator name</label>
        <div class="setrow">
          <input id="opname" name="OPERATOR_NAME" maxlength="80" placeholder="e.g. Alex" autocomplete="off">
          <button type="submit">Save</button>
        </div>
      </form>
    </div>

    <div class="setblock">
      <h3>Mode</h3>
      <p class="setdesc">Online enables the publish step <em>in the terminal</em> — this UI never posts to a live account either way.</p>
      <div class="seg" role="group" aria-label="Pipeline mode">
        <button type="button" id="mode-offline" aria-pressed="true">Offline</button>
        <button type="button" id="mode-online" aria-pressed="false">Online</button>
      </div>
    </div>

    <div class="setblock">
      <h3>Connections</h3>
      <p class="setdesc">Keys used by the terminal-side publish and scheduling skills.</p>

      <div class="conn">
        <div class="conn-head"><span class="conn-name">Upload-Post</span><span class="pill" id="pill-uploadpost">Not set</span></div>
        <p class="setdesc">Pre-audited cross-poster for TikTok / Reels / Shorts. Get a key: upload-post.com → dashboard → API keys.</p>
        <form class="setform">
          <label for="in-uploadpost">Upload-Post API key</label>
          <div class="setrow">
            <input id="in-uploadpost" name="UPLOADPOST_API_KEY" type="password" autocomplete="off" placeholder="API key">
            <button type="submit">Save</button>
          </div>
        </form>
      </div>

      <div class="conn">
        <div class="conn-head"><span class="conn-name">Scheduler</span><span class="pill" id="pill-scheduler">Not set</span></div>
        <p class="setdesc">A pre-audited scheduler (Post for Me, PostPeer, …). Base URL + API key: your provider's developer settings.</p>
        <form class="setform">
          <label for="in-scheduler-url">Scheduler base URL</label>
          <div class="setrow">
            <input id="in-scheduler-url" name="SCHEDULER_BASE_URL" type="text" autocomplete="off" placeholder="https://api.yourprovider.com">
          </div>
          <label for="in-scheduler-key">Scheduler API key</label>
          <div class="setrow">
            <input id="in-scheduler-key" name="SCHEDULER_API_KEY" type="password" autocomplete="off" placeholder="API key">
            <button type="submit">Save</button>
          </div>
        </form>
      </div>

      <div class="conn">
        <div class="conn-head"><span class="conn-name">TikTok official</span><span class="pill" id="pill-tiktok">Not set</span></div>
        <p class="setdesc">Your own app on TikTok's Content Posting API. Client key + access token: developers.tiktok.com → your app.</p>
        <form class="setform">
          <label for="in-tiktok-client">TikTok client key</label>
          <div class="setrow">
            <input id="in-tiktok-client" name="TIKTOK_CLIENT_KEY" type="password" autocomplete="off" placeholder="Client key">
          </div>
          <label for="in-tiktok-token">TikTok access token</label>
          <div class="setrow">
            <input id="in-tiktok-token" name="TIKTOK_ACCESS_TOKEN" type="password" autocomplete="off" placeholder="Access token">
            <button type="submit">Save</button>
          </div>
        </form>
      </div>

      <div class="conn">
        <div class="conn-head"><span class="conn-name">Composio</span><span class="pill" id="pill-composio">Not set</span></div>
        <p class="setdesc">Shared tool layer (Sheets, Drive, Notion, Slack). Get a key: composio.dev → dashboard → API settings.</p>
        <form class="setform">
          <label for="in-composio">Composio API key</label>
          <div class="setrow">
            <input id="in-composio" name="COMPOSIO_API_KEY" type="password" autocomplete="off" placeholder="API key">
            <button type="submit">Save</button>
          </div>
        </form>
      </div>
    </div>

    <p class="setnote">Everything here writes to <code>.env</code> (gitignored) on this machine only. Clearing a field removes its key.</p>
  </section>

  </div>
</main>

</div>
<div id="toast" role="status" aria-live="polite"></div>

<script src="/vendor/three.min.js"></script>
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
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ---------- hash router (tabs) ---------- */
const TABS = ["studio", "campaigns", "jobs", "memory", "settings"];
function route(){
  let cur = location.hash.replace("#", "");
  if(!TABS.includes(cur)) cur = "studio";
  for(const t of TABS){
    document.getElementById("sec-" + t).hidden = t !== cur;
    const tab = document.getElementById("tab-" + t);
    if(t === cur) tab.setAttribute("aria-current", "page");
    else tab.removeAttribute("aria-current");
  }
  if(cur === "settings") refreshSettings();
  if(cur === "memory") refreshMemory();
}
addEventListener("hashchange", route);

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

/* ---------- animated stat counters (state feedback, 700ms ease-out) ---------- */
const statVals = {};
function setStat(id, v){
  const el = document.getElementById(id);
  if(!el) return;
  const from = statVals[id] ?? 0;
  statVals[id] = v;
  if(REDUCED || from === v){ el.textContent = v; return; }
  const t0 = performance.now(), dur = 700;
  (function tick(t){
    const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    el.textContent = Math.round(from + (v - from) * e);
    if(k < 1) requestAnimationFrame(tick);
  })(t0);
}

/* ---------- three.js hero: sparse drifting embers, fog depth-fade ----------
   graceful CSS fallback when THREE is missing or motion is reduced */
(function heroFX(){
  const hero = document.getElementById("hero");
  const canvas = document.getElementById("fx");
  if(REDUCED || typeof THREE === "undefined"){ hero.classList.add("fallback"); return; }
  let renderer;
  try{
    renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true,
                                         powerPreference: "low-power" });
  }catch(e){ hero.classList.add("fallback"); return; }
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x131110, 0.085);
  const cam = new THREE.PerspectiveCamera(50, 2, 0.1, 60);
  cam.position.set(0, 0, 10);

  const N = 240, RANGE_Y = 9, HALF_Y = RANGE_Y / 2;
  const pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
  const y0 = new Float32Array(N), speed = new Float32Array(N), phase = new Float32Array(N);
  const cA = new THREE.Color(0xff6a2c), cB = new THREE.Color(0xffc07a);
  const c = new THREE.Color();
  for(let i = 0; i < N; i++){
    pos[i*3]   = (Math.random() * 2 - 1) * 16;
    pos[i*3+2] = -Math.random() * 9;              // depth -> fog fade
    y0[i]      = Math.random() * RANGE_Y;
    speed[i]   = 0.14 + Math.random() * 0.34;      // slow upward drift
    phase[i]   = Math.random() * Math.PI * 2;
    c.copy(cA).lerp(cB, Math.random());
    col[i*3] = c.r; col[i*3+1] = c.g; col[i*3+2] = c.b;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const mat = new THREE.PointsMaterial({
    size: 0.09, vertexColors: true, transparent: true, opacity: 0.9,
    depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true,
  });
  scene.add(new THREE.Points(geo, mat));
  const posAttr = geo.attributes.position;

  function resize(){
    const w = hero.clientWidth, h = hero.clientHeight;
    renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    cam.aspect = w / h; cam.updateProjectionMatrix();
  }
  addEventListener("resize", resize);
  resize();

  let raf = 0;
  function frame(t){
    raf = requestAnimationFrame(frame);
    const s = t * 0.001;
    for(let k = 0; k < N; k++){
      posAttr.array[k*3+1] = ((y0[k] + s * speed[k]) % RANGE_Y) - HALF_Y;
      posAttr.array[k*3]   = pos[k*3] + Math.sin(s * 0.4 + phase[k]) * 0.7;
    }
    posAttr.needsUpdate = true;
    cam.position.x = Math.sin(s * 0.05) * 0.5;
    cam.lookAt(0, 0, 0);
    renderer.render(scene, cam);
  }
  const start = () => { if(!raf) raf = requestAnimationFrame(frame); };
  const stop  = () => { cancelAnimationFrame(raf); raf = 0; };
  document.addEventListener("visibilitychange", () => document.hidden ? stop() : start());
  start();
})();

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
  return `<div class="staged">
    <div class="who"><div class="fname">${esc(u.name)}</div>
      <div class="meta">${fmtDur(u.duration)}${u.duration!=null?" · ":""}${fmtSize(u.size)} · id ${esc(u.id)}</div></div>
    <form class="assign" data-id="${esc(u.id)}">
      <input name="campaign" aria-label="Campaign name" placeholder="campaign name" value="${esc(suggestSlug(u.name))}" required>
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
      <input name="hook" aria-label="Hook title" placeholder="Hook title (on-screen, first 2s)" maxlength="120">
      <div class="row">
        <select name="grade" aria-label="Color grade"><option>vibrant</option><option>moody</option><option>none</option></select>
        <select name="reframe" aria-label="Reframe mode"><option>crop</option><option>blur</option></select>
      </div>
      <button>Produce 9:16</button>
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
    : `<li class="empty">no source video yet — assign one from staged, or drop one in Studio</li>`;
  const clips = c.clips.length ? c.clips.map(clipCard).join("")
    : `<p class="empty">No clips yet — cut moments with <code>./clip cut</code>, they show up here.</p>`;
  return `<article class="camp">
    <div class="camp-head"><span class="name">${esc(c.name)}</span>
      ${c.has_brief ? '<span class="pill brief">brief</span>' : '<span class="pill">no brief</span>'}
      <span class="meta">${c.clips.length} clip${c.clips.length===1?"":"s"}</span></div>
    <ul class="sources">${srcs}</ul>
    <div class="clips">${clips}</div></article>`;
}
async function refreshState(){
  try{
    const s = await api("/api/state");
    const badge = $("#mode");
    badge.textContent = s.mode; badge.className = "badge " + s.mode;
    setStat("stat-campaigns", s.campaigns.length);
    setStat("stat-clips", s.campaigns.reduce((n, c) => n + c.clips.length, 0));
    setStat("stat-unsorted", s.unsorted.length);
    $("#unsorted-sec").hidden = !s.unsorted.length;
    $("#unsorted").innerHTML = s.unsorted.map(unsortedRow).join("");
    $("#board").innerHTML = s.campaigns.length
      ? s.campaigns.map(campaignCard).join("")
      : `<p class="empty">No campaigns yet — drop a video in Studio, or create one below.</p>`;
  }catch(e){ toast("state: " + e.message, true); }
}

/* delegated: assign + produce + settings save + lazy content sheets */
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
  if(f.matches("form.setform")){
    e.preventDefault();
    const btn = f.querySelector("button"); btn.disabled = true;
    try{
      let saved = 0;
      for(const inp of f.querySelectorAll("input[name]")){
        if(!inp.dataset.dirty) continue;               // only send what was edited
        const s = await saveSetting(inp.name, inp.value);
        if(inp.type === "password") inp.value = "";    // secrets never linger in the DOM
        delete inp.dataset.dirty;
        applySettings(s);
        saved++;
      }
      toast(saved ? "saved — written to .env" : "nothing changed");
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
    const running = jobs.filter(j => j.status === "running").length;
    const badge = $("#jobsbadge");
    badge.hidden = !running;
    badge.textContent = running;
    $("#joblist").innerHTML = jobs.length ? jobs.map(j => `
      <div class="job">
        <div class="job-head"><span class="dot ${esc(j.status)}"></span>
          <span class="label2">${esc(j.label)}</span>
          <span class="meta">${j.elapsed}s</span></div>
        ${j.status === "running" ? '<div class="bar"><i></i></div>' : ""}
        ${j.tail ? `<pre>${esc(j.tail.split("\n").slice(-12).join("\n").trim())}</pre>` : ""}
      </div>`).join("")
      : `<p class="empty">Nothing running — produce a clip or drop a video.</p>`;
    if(stateStale){ refreshState(); refreshMemory(); }
  }catch(e){ /* server briefly busy — next poll wins */ }
}
async function refreshMemory(){
  try{
    const m = await api("/api/memory");
    $("#memlog").textContent = m.lines.join("\n");
  }catch(e){ $("#memlog").textContent = "(memory unavailable)"; }
}

/* ---------- settings ---------- */
document.addEventListener("input", e => {
  if(e.target.closest && e.target.closest("form.setform")) e.target.dataset.dirty = "1";
});
async function saveSetting(key, value){
  return api("/api/settings", { method:"POST",
    headers:{ "Content-Type":"application/json" },
    body: JSON.stringify({ key, value }) });
}
function applySettings(s){
  const chip = $("#operator");
  chip.hidden = !s.operator;
  if(s.operator) chip.textContent = "op · " + s.operator;
  const op = $("#opname");
  if(!op.dataset.dirty && document.activeElement !== op) op.value = s.operator || "";
  $("#mode-offline").setAttribute("aria-pressed", String(s.mode === "offline"));
  $("#mode-online").setAttribute("aria-pressed", String(s.mode === "online"));
  const badge = $("#mode");
  badge.textContent = s.mode; badge.className = "badge " + s.mode;
  for(const [name, b] of Object.entries(s.backends || {})){
    const pill = document.getElementById("pill-" + name);
    if(!pill) continue;
    pill.textContent = b.set ? "Connected ····" + b.last4 : "Not set";
    pill.className = "pill" + (b.set ? " on" : "");
  }
}
async function refreshSettings(){
  try{ applySettings(await api("/api/settings")); }
  catch(e){ /* transient — retried on next visit */ }
}
["offline", "online"].forEach(m => {
  document.getElementById("mode-" + m).addEventListener("click", async () => {
    try{ applySettings(await saveSetting("MODE", m)); toast("mode → " + m); }
    catch(e){ toast(e.message, true); }
  });
});

route();
refreshState(); refreshJobs(); refreshMemory(); refreshSettings();
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
    (root / "assets" / "vendor").mkdir(parents=True)
    (root / "assets" / "vendor" / "three.min.js").write_bytes(b"window.THREE={};")
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

    # 7. page structure: unique element ids, every tab + section present
    ids = re.findall(r'id="([^"]+)"', PAGE)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate element ids in PAGE: {dupes}"
    for t in ("studio", "campaigns", "jobs", "memory", "settings"):
        assert f'id="sec-{t}"' in PAGE and f'id="tab-{t}"' in PAGE, f"tab {t} missing"

    # 8. settings guardrails: allowlist, env-injection, length, MODE values
    for bad_key in ("PATH", "LD_PRELOAD", "MODE2", ""):
        try:
            api.set_setting(bad_key, "x")
            raise AssertionError(f"non-allowlisted setting accepted: {bad_key!r}")
        except ApiError as e:
            assert e.status == 400
    for bad_val in ("a\nUPLOADPOST_API_KEY=stolen", "a\rMODE=online", "x" * 501):
        try:
            api.set_setting("OPERATOR_NAME", bad_val)
            raise AssertionError(f"bad settings value accepted: {bad_val[:24]!r}")
        except ApiError as e:
            assert e.status == 400
    try:
        api.set_setting("MODE", "yolo")
        raise AssertionError("invalid MODE accepted")
    except ApiError as e:
        assert e.status == 400
    assert not (root / ".env").exists(), ".env written despite rejected values"

    # 9. live routes against the temp root (ephemeral port, 127.0.0.1)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.api = api
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(base + "/") as resp:
            assert resp.status == 200 and b"Clip Factory" in resp.read()

        # vendor three.js is served with the right type when the file exists
        with urllib.request.urlopen(base + "/vendor/three.min.js") as resp:
            assert resp.status == 200, resp.status
            assert resp.headers["Content-Type"] == "application/javascript"
            assert resp.read() == b"window.THREE={};"
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

        # settings round-trip: write, mask, update-in-place, clear
        secret = "sk-live-FAKE-abcd1234WXYZ"
        api.set_setting("OPERATOR_NAME", "Check Bot")
        api.set_setting("UPLOADPOST_API_KEY", secret)
        api.set_setting("MODE", "online")
        with urllib.request.urlopen(base + "/api/settings") as resp:
            raw = resp.read()
            assert secret.encode() not in raw, "full secret leaked to the client"
            s = json.loads(raw)
            assert set(s) == {"operator", "mode", "backends"}
            assert s["operator"] == "Check Bot" and s["mode"] == "online"
            assert s["backends"]["uploadpost"] == {"set": True, "last4": "WXYZ"}
            assert s["backends"]["composio"] == {"set": False, "last4": ""}
        envtxt = (root / ".env").read_text()
        assert f"UPLOADPOST_API_KEY={secret}" in envtxt
        assert envtxt.count("UPLOADPOST_API_KEY=") == 1
        api.set_setting("UPLOADPOST_API_KEY", secret)  # re-save: still one line
        assert (root / ".env").read_text().count("UPLOADPOST_API_KEY=") == 1
        body = json.dumps({"key": "PATH", "value": "/evil"}).encode()
        req = urllib.request.Request(base + "/api/settings", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("settings allowlist not enforced over HTTP")
        except urllib.error.HTTPError as e:
            assert e.code == 400
        api.set_setting("UPLOADPOST_API_KEY", "")      # empty value clears the key
        assert api.settings()["backends"]["uploadpost"] == {"set": False, "last4": ""}
        assert "UPLOADPOST_API_KEY" not in (root / ".env").read_text()
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
