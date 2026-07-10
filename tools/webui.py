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
/* ============ design tokens — dark is the showcase, light via media query ============ */
:root{
  --bg:#0c0a09; --bg2:#141110;
  --panel:rgba(255,255,255,.045); --panel2:rgba(255,255,255,.07);
  --border:rgba(255,255,255,.09); --border2:rgba(255,255,255,.18);
  --text:#f4f0e9; --muted:#a49a8b;
  --accent:#FF6A2C; --accent2:#ffb45e; --accent-ink:#ffa06a;
  --accent-soft:rgba(255,106,44,.13);
  --ok:#4cc47e; --err:#ff7a62;
  --r:16px; --r-sm:10px;
  --grad:linear-gradient(135deg,#FF6A2C 0%,#ff8a3c 55%,#ffb45e 100%);
  --glow:0 8px 30px rgba(255,106,44,.28);
  --shadow:0 10px 34px rgba(0,0,0,.35);
  --term-bg:#0a0908; --term-ink:#cbc1ae;
  --nav-bg:rgba(12,10,9,.82);
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#faf6f0; --bg2:#f2ebe1;
    --panel:rgba(255,255,255,.66); --panel2:rgba(40,30,20,.055);
    --border:rgba(40,30,20,.12); --border2:rgba(40,30,20,.24);
    --text:#241d15; --muted:#75675a;
    --accent-ink:#ad400f; --accent-soft:rgba(255,106,44,.10);
    --ok:#187240; --err:#b93526;
    --glow:0 8px 26px rgba(255,106,44,.25);
    --shadow:0 10px 30px rgba(40,30,20,.10);
    --nav-bg:rgba(250,246,240,.86);
  }
}
*{box-sizing:border-box;margin:0;padding:0}
html{color-scheme:dark light}
body{
  background:
    radial-gradient(1100px 520px at 50% -160px, rgba(255,106,44,.16), transparent 62%),
    radial-gradient(900px 700px at 108% 12%, rgba(255,180,94,.06), transparent 58%),
    var(--bg);
  color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
  padding-bottom:72px;
}
.wrap{max-width:1200px;margin:0 auto;padding:0 22px}
::selection{background:var(--accent-soft);color:var(--text)}

/* ============ hero ============ */
.hero{position:relative;height:38vh;min-height:300px;max-height:480px;overflow:hidden;
  background:#0c0a09;border-bottom:1px solid var(--border)}
#fx{position:absolute;inset:0;width:100%;height:100%;display:block}
.hero-veil{position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(180deg,rgba(12,10,9,.18) 0%,rgba(12,10,9,.30) 46%,var(--bg) 100%)}
.hero.fallback #fx{display:none}
.hero.fallback::before{content:"";position:absolute;inset:-45%;filter:blur(38px);
  background:
    radial-gradient(34% 44% at 28% 42%, rgba(255,106,44,.38), transparent 70%),
    radial-gradient(30% 42% at 72% 56%, rgba(255,180,94,.22), transparent 70%),
    radial-gradient(24% 34% at 52% 26%, rgba(255,70,30,.20), transparent 70%);
  animation:heroDrift 18s ease-in-out infinite alternate}
@keyframes heroDrift{to{transform:rotate(9deg) scale(1.18) translate(3%,5%)}}
.hero-inner{position:relative;z-index:2;height:100%;max-width:1200px;margin:0 auto;
  padding:0 22px;display:flex;flex-direction:column;justify-content:center;gap:10px}
.crumb{font-size:11px;font-weight:700;letter-spacing:.22em;text-transform:uppercase;
  color:rgba(255,180,120,.75)}
.wordmark{
  font-size:clamp(34px,6vw,60px);font-weight:800;letter-spacing:.14em;line-height:1.04;
  background:linear-gradient(98deg,#fff 8%,#ffd9c2 38%,#FF6A2C 72%,#ffb45e 96%);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  filter:drop-shadow(0 4px 30px rgba(255,106,44,.25));
}
.tagline{font-size:clamp(13.5px,1.6vw,16px);color:rgba(238,228,214,.82);max-width:560px}
.hero-meta{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:6px}
.badge{
  font-size:10.5px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  padding:5px 13px;border-radius:999px;border:1px solid rgba(255,255,255,.22);
  color:rgba(255,255,255,.75);background:rgba(255,255,255,.06);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
}
.badge.online{color:#7fe0a8;border-color:rgba(127,224,168,.5)}
.badge.offline{color:#ffb090;border-color:rgba(255,176,144,.5)}
.hero .hint{font-size:12px;color:rgba(238,228,214,.5)}

/* ============ top nav (section tabs) ============ */
.topnav{position:sticky;top:0;z-index:40;border-bottom:1px solid var(--border);
  background:var(--nav-bg);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.tabs{display:flex;gap:2px;overflow-x:auto}
.tab{display:inline-flex;align-items:center;gap:8px;padding:13px 16px 11px;
  font-size:13px;font-weight:650;letter-spacing:.02em;color:var(--muted);
  text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap;
  transition:color .2s, border-color .2s}
.tab:hover{color:var(--text)}
.tab[aria-current]{color:var(--accent-ink);border-bottom-color:var(--accent)}
.tab:focus-visible,button:focus-visible,summary:focus-visible{
  outline:2px solid var(--accent);outline-offset:2px}
.tabbadge{min-width:19px;height:19px;padding:0 6px;border-radius:999px;
  background:var(--grad);color:#180b04;font-size:11px;font-weight:800;
  display:inline-grid;place-items:center;line-height:1}
[hidden]{display:none !important}
.view{padding-top:8px}
.narrow{max-width:820px}
.block{margin-bottom:28px}

/* ============ shared bits ============ */
section{margin-bottom:28px}
h2.label{
  font-size:11px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
  color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:8px;
}
h2.label::before{content:"";width:16px;height:2px;border-radius:2px;background:var(--grad)}
.card{
  background:var(--panel);border:1px solid var(--border);border-radius:var(--r);
  padding:18px;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.05), var(--shadow);
  transition:transform .25s ease, border-color .25s ease, box-shadow .25s ease;
}
.card:hover{border-color:var(--border2)}
.empty{color:var(--muted);font-size:13.5px;padding:10px 2px}
code{background:var(--panel2);border:1px solid var(--border);border-radius:6px;
  padding:1px 6px;font-size:12.5px}
button{
  font:inherit;font-size:13.5px;font-weight:600;color:var(--text);
  background:var(--panel2);border:1px solid var(--border);border-radius:10px;
  padding:8px 15px;cursor:pointer;
  transition:border-color .2s, color .2s, box-shadow .25s, transform .15s;
}
button:hover{border-color:var(--accent);color:var(--accent-ink);transform:translateY(-1px)}
button.primary{background:var(--grad);border:1px solid transparent;color:#180b04}
button.primary:hover{color:#180b04;box-shadow:var(--glow)}
button:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
input,select,textarea{
  font:inherit;font-size:13.5px;color:var(--text);background:var(--bg2);
  border:1px solid var(--border);border-radius:10px;padding:8px 11px;min-width:0;
  transition:border-color .2s, box-shadow .2s;
}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
input::placeholder{color:var(--muted);opacity:.7}

/* ============ entrance motion ============ */
.reveal{opacity:0;transform:translateY(16px);
  transition:opacity .65s cubic-bezier(.2,.7,.3,1), transform .65s cubic-bezier(.2,.7,.3,1)}
.reveal.in{opacity:1;transform:none}

/* ============ stats strip ============ */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:26px 0 30px}
@media(max-width:640px){.stats{grid-template-columns:1fr}}
.stat{padding:18px 20px;position:relative;overflow:hidden}
.stat::after{content:"";position:absolute;right:-30px;top:-30px;width:110px;height:110px;
  border-radius:50%;background:radial-gradient(closest-side, var(--accent-soft), transparent);pointer-events:none}
.stat .num{
  font-size:34px;font-weight:800;letter-spacing:-.02em;line-height:1.1;
  background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent;
  font-variant-numeric:tabular-nums;
}
.stat .cap{font-size:11.5px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;
  color:var(--muted);margin-top:4px}

/* ============ layout ============ */
main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:22px}
@media(max-width:960px){main{grid-template-columns:1fr}}

/* ============ drop zone ============ */
.drop{
  border:1.5px dashed var(--border2);border-radius:20px;background:var(--panel);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  padding:46px 24px;text-align:center;margin-bottom:28px;position:relative;overflow:hidden;
  transition:border-color .2s, background .2s, box-shadow .3s;
}
.drop::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:0;
  background:radial-gradient(60% 120% at 50% 0%, var(--accent-soft), transparent 70%);
  transition:opacity .3s}
.drop.over{border-color:var(--accent);animation:dropPulse 1.1s ease-in-out infinite}
.drop.over::before{opacity:1}
@keyframes dropPulse{
  0%,100%{box-shadow:0 0 0 0 rgba(255,106,44,.0), 0 0 26px 2px rgba(255,106,44,.18)}
  50%{box-shadow:0 0 0 4px rgba(255,106,44,.14), 0 0 42px 8px rgba(255,106,44,.30)}
}
.drop .glyph{
  width:52px;height:52px;margin:0 auto 14px;border-radius:15px;
  background:var(--grad);color:#180b04;display:grid;place-items:center;
  font-size:22px;font-weight:800;box-shadow:var(--glow);
}
.drop .t1{font-weight:700;font-size:17px;letter-spacing:-.01em}
.drop .t2{color:var(--muted);font-size:13px;margin:6px 0 16px}

/* ============ staged / unsorted ============ */
.staged{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:12px}
.staged .who{flex:1;min-width:180px}
.fname{font-weight:650;font-size:14px;word-break:break-all}
.meta{color:var(--muted);font-size:12.5px}
form.assign{display:flex;gap:8px}

/* ============ campaign board ============ */
.camp{margin-bottom:18px}
.camp-head{display:flex;align-items:baseline;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.camp-head .name{font-size:18px;font-weight:750;letter-spacing:-.015em}
.pill{
  font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  border:1px solid var(--border);color:var(--muted);border-radius:999px;padding:2.5px 9px;
}
.pill.final{color:var(--ok);border-color:currentColor;background:rgba(76,196,126,.08)}
.pill.brief{color:var(--accent-ink);border-color:currentColor;background:var(--accent-soft)}
ul.sources{list-style:none;margin-bottom:14px}
ul.sources li{display:flex;gap:10px;align-items:baseline;padding:3px 0;flex-wrap:wrap}
.clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.clip{background:var(--panel2);border:1px solid var(--border);border-radius:14px;
  padding:10px;display:flex;flex-direction:column;gap:8px;
  transition:transform .25s ease, border-color .25s ease, box-shadow .3s ease}
.clip:hover{transform:translateY(-3px);border-color:var(--border2);
  box-shadow:0 14px 34px rgba(0,0,0,.35), 0 0 0 1px var(--accent-soft)}
.clip video{width:100%;aspect-ratio:9/16;max-height:340px;border-radius:10px;background:#000}
.clip-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.clip-head .fname{flex:1;font-size:12.5px}
details{border-top:1px solid var(--border);padding-top:6px}
details summary{cursor:pointer;font-size:12.5px;color:var(--muted);user-select:none;
  transition:color .2s}
details summary:hover{color:var(--accent-ink)}
details img{width:100%;border-radius:8px;margin-top:8px}
details pre{
  margin-top:8px;font-size:11.5px;line-height:1.5;white-space:pre-wrap;
  word-break:break-word;max-height:260px;overflow:auto;color:var(--text);
  background:var(--bg2);border-radius:8px;padding:10px;
}
form.produce{display:flex;flex-direction:column;gap:7px;border-top:1px solid var(--border);padding-top:9px}
form.produce .row{display:flex;gap:7px}
form.produce select{flex:1}

/* ============ new campaign ============ */
#newcamp form{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:640px){#newcamp form{grid-template-columns:1fr}}
#newcamp .full{grid-column:1/-1}
#newcamp label{display:flex;flex-direction:column;gap:5px;font-size:12.5px;color:var(--muted)}
#newcamp .check{flex-direction:row;align-items:center;gap:8px;color:var(--text)}
#newcamp input[type=checkbox]{accent-color:var(--accent);width:16px;height:16px}
#newcamp .actions{grid-column:1/-1;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.fieldnote{font-size:11.5px;color:var(--muted)}
.fieldnote.bad{color:var(--err);font-weight:600}

/* ============ jobs rail ============ */
.job{margin-bottom:12px;padding:14px}
.job-head{display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.dot.running{background:var(--accent);box-shadow:0 0 10px 1px rgba(255,106,44,.6);
  animation:pulse 1.1s ease-in-out infinite}
.dot.done{background:var(--ok)} .dot.error{background:var(--err)}
@keyframes pulse{50%{opacity:.35}}
.job .label2{flex:1;font-size:13px;font-weight:600;word-break:break-all}
.job .bar{height:3px;border-radius:3px;overflow:hidden;background:var(--panel2);
  margin-top:10px;position:relative}
.job .bar i{position:absolute;inset:0;transform:translateX(-100%);
  background:linear-gradient(90deg,transparent,var(--accent) 45%,var(--accent2) 55%,transparent);
  animation:shimmer 1.5s linear infinite}
@keyframes shimmer{to{transform:translateX(100%)}}
.job pre{
  margin-top:8px;font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;
  max-height:150px;overflow:auto;background:var(--bg2);border-radius:8px;padding:8px;
  color:var(--muted);
}

/* ============ memory terminal ============ */
.term{background:var(--term-bg);border:1px solid var(--border);border-radius:var(--r);
  padding:0;overflow:hidden;position:relative;box-shadow:var(--shadow)}
.term::after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.6;
  background:repeating-linear-gradient(0deg, rgba(255,255,255,.028) 0 1px, transparent 1px 3px)}
.term .tbar{display:flex;align-items:center;gap:6px;padding:10px 13px;
  border-bottom:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.03)}
.term .tbar b{width:9px;height:9px;border-radius:50%;display:inline-block}
.term .tbar b:nth-child(1){background:#ff5f57}.term .tbar b:nth-child(2){background:#febc2e}
.term .tbar b:nth-child(3){background:#28c840}
.term .tbar span{margin-left:8px;font:11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  color:rgba(255,255,255,.42);letter-spacing:.05em}
pre.mem{
  font:11.5px/1.7 ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  white-space:pre-wrap;word-break:break-word;color:var(--term-ink);
  max-height:420px;overflow:auto;padding:13px 15px;
}
pre.mem::after{content:"▌";color:var(--accent);animation:blink 1.15s steps(1) infinite}
@keyframes blink{50%{opacity:0}}

/* ============ settings ============ */
.setcard{margin-bottom:18px}
.setcard h3{font-size:15px;font-weight:750;letter-spacing:-.01em;margin-bottom:3px}
.setdesc{font-size:12.5px;color:var(--muted);margin-bottom:10px}
form.setform{display:flex;flex-direction:column;gap:6px}
form.setform label{font-size:12px;font-weight:600;color:var(--muted)}
.setrow{display:flex;gap:8px}
.setrow input{flex:1}
.seg{display:inline-flex;gap:3px;padding:3px;border:1px solid var(--border);
  border-radius:12px;background:var(--bg2)}
.seg button{border-color:transparent;background:transparent;padding:7px 20px;border-radius:9px}
.seg button[aria-pressed="true"]{background:var(--grad);color:#180b04}
.seg button[aria-pressed="true"]:hover{color:#180b04;transform:none}
.conn{padding:14px 0 8px;border-top:1px solid var(--border);margin-top:12px}
.conn-head{display:flex;align-items:center;gap:10px;margin-bottom:2px}
.conn-name{font-weight:700;font-size:14px}
.pill.on{color:var(--ok);border-color:currentColor;background:rgba(76,196,126,.08)}
.setnote{font-size:12px;color:var(--muted);padding:2px 4px}

/* ============ toast ============ */
#toast{
  position:fixed;left:50%;bottom:26px;transform:translate(-50%,20px);opacity:0;z-index:50;
  background:rgba(20,17,16,.92);color:#f4f0e9;border:1px solid var(--border2);
  backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
  font-size:13.5px;font-weight:600;padding:11px 20px;border-radius:12px;
  pointer-events:none;transition:.28s;max-width:80vw;box-shadow:var(--shadow);
}
#toast.show{opacity:1;transform:translate(-50%,0)}
#toast.err{background:rgba(120,30,18,.94);border-color:rgba(255,122,98,.5);color:#fff}

/* ============ reduced motion: calm everything ============ */
@media (prefers-reduced-motion: reduce){
  *,*::before,*::after{animation:none !important;transition:none !important}
  .reveal{opacity:1;transform:none}
  .clip:hover,button:hover{transform:none}
}
</style>
</head>
<body>

<div class="hero" id="hero">
  <canvas id="fx" aria-hidden="true"></canvas>
  <div class="hero-veil"></div>
  <div class="hero-inner">
    <div class="crumb">Vyro · short-form pipeline</div>
    <h1 class="wordmark">CLIP FACTORY</h1>
    <p class="tagline">Approved campaign video in — scroll-stopping vertical clips out.</p>
    <div class="hero-meta">
      <span id="mode" class="badge">…</span>
      <span id="operator" class="badge" hidden></span>
      <span class="hint">local · 127.0.0.1 · publishing stays in the terminal</span>
    </div>
  </div>
</div>

<nav class="topnav" aria-label="Sections">
  <div class="wrap tabs">
    <a class="tab" id="tab-studio" href="#studio">Studio</a>
    <a class="tab" id="tab-campaigns" href="#campaigns">Campaigns</a>
    <a class="tab" id="tab-jobs" href="#jobs">Jobs<span class="tabbadge" id="jobsbadge" hidden>0</span></a>
    <a class="tab" id="tab-memory" href="#memory">Memory</a>
    <a class="tab" id="tab-settings" href="#settings">Settings</a>
  </div>
</nav>

<div class="wrap">
  <section class="view" id="sec-studio">
    <div class="stats">
      <div class="stat card reveal"><div class="num" id="stat-campaigns">0</div><div class="cap">Campaigns</div></div>
      <div class="stat card reveal"><div class="num" id="stat-clips">0</div><div class="cap">Clips produced</div></div>
      <div class="stat card reveal"><div class="num" id="stat-unsorted">0</div><div class="cap">Awaiting sort</div></div>
    </div>

    <div id="drop" class="drop reveal">
      <div class="glyph">▼</div>
      <div class="t1">Drop a campaign video to start</div>
      <div class="t2">.mp4 · .mov · .mkv · .webm · .avi · .m4v — saved to <code>inbox/</code>, then ingested automatically</div>
      <button id="pick" type="button">Choose files…</button>
      <input type="file" id="file" multiple accept=".mp4,.mov,.mkv,.webm,.avi,.m4v,video/*" hidden aria-label="Choose video files">
    </div>

    <div class="block" id="unsorted-sec" hidden>
      <h2 class="label">Staged — needs a campaign</h2>
      <div id="unsorted"></div>
    </div>
  </section>

  <section class="view" id="sec-campaigns" hidden>
    <div class="block">
      <h2 class="label">Campaign board</h2>
      <div id="board"><div class="empty">Loading…</div></div>
    </div>

    <div class="card reveal block" id="newcamp">
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
    </div>
  </section>

  <section class="view narrow" id="sec-jobs" hidden>
    <h2 class="label">Jobs</h2>
    <div id="joblist"><div class="empty">Nothing running — produce a clip or drop a video.</div></div>
  </section>

  <section class="view narrow" id="sec-memory" hidden>
    <h2 class="label">Memory</h2>
    <div class="term reveal">
      <div class="tbar"><b></b><b></b><b></b><span>clip mem recall</span></div>
      <pre class="mem" id="memlog">…</pre>
    </div>
  </section>

  <section class="view narrow" id="sec-settings" hidden>
    <h2 class="label">Settings</h2>

    <div class="card setcard reveal">
      <h3>Operator</h3>
      <p class="setdesc">Who is running this factory — shown as a chip in the header and used to attribute updates.</p>
      <form class="setform" id="opform">
        <label for="opname">Operator name</label>
        <div class="setrow">
          <input id="opname" name="OPERATOR_NAME" maxlength="80" placeholder="e.g. Alex" autocomplete="off">
          <button class="primary" type="submit">Save</button>
        </div>
      </form>
    </div>

    <div class="card setcard reveal">
      <h3>Mode</h3>
      <p class="setdesc">Online enables the publish step <em>in the terminal</em> — this UI never posts to a live account either way.</p>
      <div class="seg" role="group" aria-label="Pipeline mode">
        <button type="button" id="mode-offline" aria-pressed="true">Offline</button>
        <button type="button" id="mode-online" aria-pressed="false">Online</button>
      </div>
    </div>

    <div class="card setcard reveal">
      <h3>Connections</h3>
      <p class="setdesc">Keys used by the terminal-side publish and scheduling skills. Saved values are never sent back to the browser — only the last 4 characters.</p>

      <div class="conn">
        <div class="conn-head"><span class="conn-name">Upload-Post</span><span class="pill" id="pill-uploadpost">Not set</span></div>
        <p class="setdesc">Pre-audited cross-poster for TikTok / Reels / Shorts. Get a key: upload-post.com → dashboard → API keys.</p>
        <form class="setform">
          <label for="in-uploadpost">Upload-Post API key</label>
          <div class="setrow">
            <input id="in-uploadpost" name="UPLOADPOST_API_KEY" type="password" autocomplete="off" placeholder="API key">
            <button class="primary" type="submit">Save</button>
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
            <button class="primary" type="submit">Save</button>
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
            <button class="primary" type="submit">Save</button>
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
            <button class="primary" type="submit">Save</button>
          </div>
        </form>
      </div>
    </div>

    <p class="setnote reveal">Danger-free zone: everything here writes to <code>.env</code> (gitignored) on this machine only. Nothing is uploaded anywhere, and clearing a field removes its key.</p>
  </section>
</div>
<div id="toast"></div>

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
  revealIn(document.getElementById("sec-" + cur));
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

/* ---------- entrance motion (IntersectionObserver + CSS transitions) ---------- */
let booted = false;   // after the first paint settles, new renders appear instantly
const io = (!REDUCED && "IntersectionObserver" in window)
  ? new IntersectionObserver(entries => {
      for(const en of entries){
        if(en.isIntersecting){ en.target.classList.add("in"); io.unobserve(en.target); }
      }
    }, { rootMargin: "0px 0px -6% 0px" })
  : null;
function revealIn(scope){
  const els = (scope || document).querySelectorAll(".reveal:not(.in)");
  let i = 0;
  for(const el of els){
    if(el.closest("[hidden]")) continue;   // revealed by route() when its tab opens
    if(!io || booted){ el.classList.add("in"); continue; }
    el.style.transitionDelay = Math.min(i++ * 70, 420) + "ms";
    io.observe(el);
  }
}
revealIn(document);
setTimeout(() => { booted = true; }, 2500);

/* ---------- animated stat counters ---------- */
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

/* ---------- three.js hero (graceful fallback if the vendor file is missing) ---------- */
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
  scene.fog = new THREE.FogExp2(0x0c0a09, 0.052);
  const cam = new THREE.PerspectiveCamera(55, 2, 0.1, 100);
  cam.position.set(0, 4.4, 11);

  // a drifting wave-field of glowing orange->amber particles
  const COLS = 128, ROWS = 46, SX = 0.36, SZ = 0.44, N = COLS * ROWS;
  const pos = new Float32Array(N * 3), col = new Float32Array(N * 3);
  const phase = new Float32Array(N);
  const cA = new THREE.Color(0xff6a2c), cB = new THREE.Color(0xffb45e);
  const c = new THREE.Color();
  let i = 0;
  for(let r = 0; r < ROWS; r++) for(let q = 0; q < COLS; q++){
    pos[i*3]   = (q - COLS/2) * SX;
    pos[i*3+1] = 0;
    pos[i*3+2] = (r - ROWS/2) * SZ;
    c.copy(cA).lerp(cB, Math.min(1, q/COLS * .85 + Math.random() * .25));
    col[i*3] = c.r; col[i*3+1] = c.g; col[i*3+2] = c.b;
    phase[i] = Math.random() * Math.PI * 2;
    i++;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
  const mat = new THREE.PointsMaterial({
    size: 0.075, vertexColors: true, transparent: true, opacity: 0.85,
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
    const s = t * 0.00045;
    for(let k = 0; k < N; k++){
      const x = pos[k*3], z = pos[k*3+2];
      posAttr.array[k*3+1] =
        Math.sin(x * 0.55 + s * 2.2) * 0.55 +
        Math.cos(z * 0.50 - s * 1.6) * 0.45 +
        Math.sin(phase[k] + s * 3.0) * 0.08;
    }
    posAttr.needsUpdate = true;
    cam.position.x = Math.sin(s * 0.5) * 0.7;
    cam.lookAt(0, 0.35, 0);
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
  return `<div class="card staged reveal">
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
  return `<div class="card camp reveal">
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
    setStat("stat-campaigns", s.campaigns.length);
    setStat("stat-clips", s.campaigns.reduce((n, c) => n + c.clips.length, 0));
    setStat("stat-unsorted", s.unsorted.length);
    $("#unsorted-sec").hidden = !s.unsorted.length;
    $("#unsorted").innerHTML = s.unsorted.map(unsortedRow).join("");
    $("#board").innerHTML = s.campaigns.length
      ? s.campaigns.map(campaignCard).join("")
      : `<div class="empty">No campaigns yet — drop a video to start, or create one below.</div>`;
    revealIn($("#unsorted")); revealIn($("#board"));
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
      <div class="card job">
        <div class="job-head"><span class="dot ${esc(j.status)}"></span>
          <span class="label2">${esc(j.label)}</span>
          <span class="meta">${j.elapsed}s</span></div>
        ${j.status === "running" ? '<div class="bar"><i></i></div>' : ""}
        ${j.tail ? `<pre>${esc(j.tail.split("\n").slice(-12).join("\n").trim())}</pre>` : ""}
      </div>`).join("")
      : `<div class="empty">Nothing running — produce a clip or drop a video.</div>`;
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
  if(s.operator) chip.textContent = "Operator: " + s.operator;
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
