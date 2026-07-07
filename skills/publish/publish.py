#!/usr/bin/env python3
"""
publish.py — prepare and publish a finished clip the LEGITIMATE way.

Two publish backends, both official/permissioned:
  --via official    TikTok Content Posting API (needs YOUR audited app + token)
  --via scheduler   a pre-audited unified scheduler you connect once via OAuth

This script will NEVER drive a browser, mimic clicks, use anti-detect tooling, or
post to multiple/throwaway accounts. Those break TikTok's rules and get accounts
banned. It defaults to DRY-RUN: it prints exactly what it would send and changes
nothing until you pass --send.

Credentials come from env vars (see SKILL.md). Nothing is hard-coded.
"""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

TIKTOK_BASE = "https://open.tiktokapis.com"
LEDGER = Path(__file__).resolve().parents[2] / "knowledge" / "ledger.md"


# ---------------- prepare ----------------

def cmd_prepare(args):
    brief = json.loads(Path(args.brief).read_text()) if args.brief else {}
    clip = Path(args.clip)
    if not clip.exists():
        sys.exit(f"clip not found: {clip}")

    caption = args.caption or brief.get("caption_template", "")
    hashtags = brief.get("required_hashtags", [])
    package = {
        "video": str(clip),
        "campaign": brief.get("campaign", clip.parent.name),
        "caption": caption.strip(),
        "hashtags": hashtags,
        "required_hashtags": hashtags,             # from brief; gate checks these survive edits
        "privacy": "public",                       # Vyro needs public views
        "ai_generated": bool(brief.get("ai_generated", True)),  # disclose AI by default
        "disclosure": brief.get("disclosure", "#ad" if brief.get("is_branded") else ""),
        "video_url": brief.get("video_url", ""),   # public URL; official + composio pull from this
        "platforms": brief.get("platforms", ["tiktok", "reels", "shorts"]),
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
    }
    Path(args.out).write_text(json.dumps(package, indent=2))
    print(f"wrote {args.out}")
    print(json.dumps(package, indent=2))
    print("\nNext: python3 publish.py publish --package", args.out,
          "--via scheduler   (add --send when ready)")


# ---------------- compliance gate ----------------

def compliance_check(pkg):
    """Return list of problems; empty list means OK to send."""
    problems = []
    if pkg.get("privacy") != "public":
        problems.append("privacy is not public — would earn 0 Vyro views")
    final = build_caption(pkg).lower()
    if pkg.get("ai_generated") and "ai" not in final:
        # not authoritative — TikTok also has a native AIGC toggle; set that too.
        problems.append("AI content but no AI disclosure in caption/label — add the AI-content label")
    missing = [h for h in pkg.get("required_hashtags", [])
               if h.lower().lstrip("#") not in final]
    if missing:
        problems.append(f"missing required hashtags from brief: {missing}")
    dur = ffprobe_duration(pkg.get("video", ""))
    if dur is not None and dur < 3:
        problems.append(f"clip is {dur:.1f}s — TikTok rejects under 3s")
    return problems


def ffprobe_duration(path):
    if not path or not Path(path).exists():
        return None
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        stdout=subprocess.PIPE, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


# ---------------- publish ----------------

def cmd_publish(args):
    pkg = json.loads(Path(args.package).read_text())

    problems = compliance_check(pkg)
    if problems:
        print("COMPLIANCE CHECK FAILED — not sending:")
        for p in problems:
            print("  -", p)
        if not args.force:
            sys.exit(1)
        print("(--force given; continuing anyway)")
    else:
        print("compliance check: OK")

    caption = build_caption(pkg)
    print(f"\nWould publish: {pkg['video']}")
    print(f"  platforms : {', '.join(pkg.get('platforms', []))}")
    print(f"  privacy   : {pkg.get('privacy')}")
    print(f"  caption   : {caption!r}")

    if not args.send:
        print("\nDRY-RUN (no --send): nothing was published.")
        return

    if not args.auto:
        ans = input("\nPublish this now? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted.")
            return

    if args.via == "official":
        url = publish_official(pkg, caption)
    elif args.via == "composio":
        url = publish_composio(pkg, caption)
    elif args.via == "uploadpost":
        url = publish_uploadpost(pkg, caption)
    elif args.via == "queue":
        path = publish_queue(pkg, caption)
        print(f"\nqueued for posting: {path}")
        print("drag the .mp4 into TikTok/Reels/Shorts and paste caption.txt,")
        print("then add the live URL to knowledge/ledger.md (./clip dashboard tracks it).")
        return
    else:
        url = publish_scheduler(pkg, caption)

    if url:
        log_to_ledger(pkg, url)
        print(f"\npublished: {url}\nlogged to ledger -> submit this URL in Vyro.")


def build_caption(pkg):
    parts = [pkg.get("caption", "").strip()]
    if pkg.get("disclosure"):
        parts.append(pkg["disclosure"])
    parts += pkg.get("hashtags", [])
    return " ".join(p for p in parts if p).strip()


def _post_json(url, token, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        method="POST")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode())


def publish_official(pkg, caption):
    """TikTok Content Posting API, Direct Post via PULL_FROM_URL.

    Requires a PUBLICLY reachable video URL (host the export somewhere TikTok can
    fetch). Needs your AUDITED app's token with scope video.publish, or every post
    stays private. Field names can drift — verify against current TikTok docs.
    """
    token = os.environ.get("TIKTOK_ACCESS_TOKEN")
    if not token:
        sys.exit("set TIKTOK_ACCESS_TOKEN (scope video.publish) to use --via official")
    video_url = pkg.get("video_url")
    if not video_url:
        sys.exit("official API needs a public 'video_url' in the package (host the file first)")

    # 1) creator info (required before posting; also returns allowed privacy levels)
    info = _post_json(f"{TIKTOK_BASE}/v2/post/publish/creator_info/query/", token, {})
    print("creator_info:", json.dumps(info)[:200])

    # 2) init the post
    init = _post_json(f"{TIKTOK_BASE}/v2/post/publish/video/init/", token, {
        "post_info": {
            "title": caption[:2200],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_comment": False,
            # TikTok also exposes an AI-generated-content flag; set it per current
            # docs so the AIGC label is applied. Field name has changed before.
        },
        "source_info": {"source": "PULL_FROM_URL", "video_url": video_url},
    })
    publish_id = (init.get("data") or {}).get("publish_id")
    if not publish_id:
        sys.exit(f"init failed: {json.dumps(init)[:300]}")

    # 3) poll status
    for _ in range(30):
        st = _post_json(f"{TIKTOK_BASE}/v2/post/publish/status/fetch/", token,
                        {"publish_id": publish_id})
        status = (st.get("data") or {}).get("status")
        print("status:", status)
        if status == "PUBLISH_COMPLETE":
            ids = (st.get("data") or {}).get("publicaly_available_post_id") or []
            return f"https://www.tiktok.com/@me/video/{ids[0]}" if ids else "PUBLISH_COMPLETE"
        if status in ("FAILED", "PUBLISH_FAILED"):
            sys.exit(f"publish failed: {json.dumps(st)[:300]}")
        time.sleep(5)
    print("timed out polling status; check your TikTok inbox/profile.")
    return None


def publish_scheduler(pkg, caption):
    """Publish via a pre-audited unified scheduler (provider-agnostic).

    Set SCHEDULER_BASE_URL + SCHEDULER_API_KEY. The exact body varies by provider;
    this is the common shape (video URL + caption + platforms). Adjust to your provider.
    """
    base = os.environ.get("SCHEDULER_BASE_URL")
    key = os.environ.get("SCHEDULER_API_KEY")
    if not (base and key):
        sys.exit("set SCHEDULER_BASE_URL and SCHEDULER_API_KEY to use --via scheduler")
    body = {
        "media_url": pkg.get("video_url") or pkg["video"],
        "caption": caption,
        "platforms": pkg.get("platforms", ["tiktok"]),
        "visibility": "public",
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/posts",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read().decode())
    return resp.get("url") or resp.get("post_url") or json.dumps(resp)[:200]


def publish_composio(pkg, caption):
    """Publish to TikTok through Composio's TikTok toolkit.

    Composio's TIKTOK_PUBLISH_VIDEO wraps TikTok's official Content Posting API and
    pulls the video from a public URL. You connect your OWN TikTok credentials in the
    Composio dashboard; Composio manages the OAuth lifecycle. Because it's the official
    API underneath, the same audit/visibility rules apply (your app must be audited for
    public posts) — this is a managed-auth route, NOT a way around those rules.

    Needs COMPOSIO_API_KEY, a connected TikTok account, and a public video_url.
    Confirm the current tool slug/params in the Composio dashboard; the SDK call below
    may need adjusting to your installed composio version.
    """
    key = os.environ.get("COMPOSIO_API_KEY")
    if not key:
        sys.exit("set COMPOSIO_API_KEY to use --via composio (see skills/composio/SKILL.md)")
    video_url = pkg.get("video_url")
    if not video_url:
        sys.exit("composio TIKTOK_PUBLISH_VIDEO pulls from a public URL — add 'video_url' "
                 "to the package (host the export somewhere TikTok can reach it).")
    try:
        from composio import Composio
    except Exception:
        sys.exit("Composio SDK not installed:  .venv/bin/pip install composio")

    composio = Composio(api_key=key)
    user_id = os.environ.get("COMPOSIO_USER_ID", "default")
    tool = os.environ.get("COMPOSIO_TIKTOK_TOOL", "TIKTOK_PUBLISH_VIDEO")
    arguments = {
        "video_url": video_url,
        "title": caption[:2200],
        "privacy_level": "PUBLIC_TO_EVERYONE",
    }
    print(f"composio: executing {tool} for user '{user_id}'")
    result = composio.tools.execute(tool, user_id=user_id, arguments=arguments)
    blob = result if isinstance(result, str) else json.dumps(result)
    import re as _re
    m = _re.search(r"https?://[^\s\"']+tiktok[^\s\"']*", blob)
    return m.group(0) if m else (blob[:200] or "submitted (check TikTok status)")


def publish_queue(pkg, caption):
    """FREE, zero-setup 'publish': assemble a ready-to-post pack locally.

    No account, no API, no audit, no ban risk. You get a folder with the clip +
    caption.txt to drag into TikTok/Reels/Shorts. This is the easiest free path.
    """
    video = Path(pkg["video"])
    campaign = pkg.get("campaign", "misc")
    ready = Path("out") / campaign / "ready" / video.stem
    ready.mkdir(parents=True, exist_ok=True)
    dest = ready / video.name
    if video.exists() and video.resolve() != dest.resolve():
        shutil.copy2(video, dest)
    (ready / "caption.txt").write_text(caption + "\n")
    (ready / "post.json").write_text(json.dumps(pkg, indent=2))
    q = Path("knowledge") / "queue.md"
    q.parent.mkdir(parents=True, exist_ok=True)
    if not q.exists():
        q.write_text("# To post\n\nOne line per ready clip. Tick it once it's live, "
                     "then add the URL to ledger.md.\n\n")
    with q.open("a") as f:
        f.write(f"- [ ] {campaign}: `{dest}`  ·  caption: `{ready/'caption.txt'}`\n")
    return str(ready)


def publish_uploadpost(pkg, caption):
    """FREE auto-post via Upload-Post (api.upload-post.com).

    A pre-audited partner, so you skip TikTok's audit entirely — free tier is ~10
    uploads/month and it cross-posts to TikTok/Reels/Shorts. Uploads the file directly
    (no public hosting needed). Set UPLOADPOST_API_KEY. Confirm current field names at
    upload-post.com docs; this uses the documented multipart upload shape.
    """
    key = os.environ.get("UPLOADPOST_API_KEY")
    if not key:
        sys.exit("set UPLOADPOST_API_KEY to use --via uploadpost (free tier at upload-post.com)")
    video = pkg["video"]
    if not Path(video).exists():
        sys.exit(f"video not found: {video}")
    try:
        import requests
    except Exception:
        sys.exit("install requests:  .venv/bin/pip install requests")
    plat_map = {"reels": "instagram", "shorts": "youtube"}
    data = [("title", caption[:2200]), ("add_to_queue", "true")]
    for p in pkg.get("platforms", ["tiktok"]):
        data.append(("platform[]", plat_map.get(p, p)))
    if pkg.get("ai_generated"):
        data.append(("is_aigc", "true"))          # AI-content disclosure label
    if pkg.get("disclosure", "").lower() == "#ad":
        data.append(("brand_content_toggle", "true"))
    with open(video, "rb") as fh:
        resp = requests.post("https://api.upload-post.com/api/upload",
                             headers={"Authorization": f"Apikey {key}"},
                             data=data, files={"video": fh}, timeout=180)
    try:
        j = resp.json()
    except Exception:
        j = {"status": resp.status_code, "text": resp.text[:200]}
    blob = json.dumps(j)
    import re as _re
    m = _re.search(r"https?://[^\s\"']+", blob)
    return m.group(0) if m else blob[:200]


def log_to_ledger(pkg, url):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    if not LEDGER.exists():
        LEDGER.write_text("# Ledger\n\n| date | campaign | clip | platform | url | views |\n"
                          "|------|----------|------|----------|-----|-------|\n")
    date = datetime.now().strftime("%Y-%m-%d")
    plats = "/".join(pkg.get("platforms", []))
    row = f"| {date} | {pkg.get('campaign','')} | {Path(pkg['video']).name} | {plats} | {url} |  |\n"
    with LEDGER.open("a") as f:
        f.write(row)


def main():
    ap = argparse.ArgumentParser(description="Prepare/publish a clip the legitimate way.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="build a post package from a clip + brief")
    p.add_argument("--clip", required=True)
    p.add_argument("--brief", help="campaign brief JSON")
    p.add_argument("--caption", help="override caption")
    p.add_argument("--out", default="post_package.json")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("publish", help="publish a prepared package (dry-run unless --send)")
    p.add_argument("--package", required=True)
    p.add_argument("--via", choices=["queue", "uploadpost", "scheduler", "official", "composio"],
                   default="queue", help="queue=free local pack; uploadpost=free API; others need creds")
    p.add_argument("--send", action="store_true", help="actually publish (off by default)")
    p.add_argument("--auto", action="store_true", help="skip the confirm prompt")
    p.add_argument("--force", action="store_true", help="publish even if compliance fails")
    p.set_defaults(func=cmd_publish)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
