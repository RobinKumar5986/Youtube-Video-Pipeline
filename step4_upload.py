#!/usr/bin/env python3
"""
SortCliper - Step 4: Generate metadata with local Ollama, then upload each
rendered Shorts clip to YouTube.

Setup (one-time, per channel):
1. Google Cloud Console → enable "YouTube Data API v3".
2. Credentials → Create Credentials → OAuth client ID → Application type: Desktop app.
   Download the JSON and save it next to this file.

   MULTI-CHANNEL SUPPORT: you can add one client-secret file per YouTube
   channel you want to upload to. Any file matching client_secret*.json is
   picked up automatically. Naming convention:

       client_secret.json              → channel labeled "Default"
       client_secret_<name>.json       → channel labeled "<name>"
       client_secret<name>.json        → channel labeled "<name>"  (e.g. client_secret1.json → "1")

   e.g. client_secret_gaming.json (or client_secret2.json) becomes its own
   channel, and gets its own cached token file (token_gaming.json /
   token_2.json) so re-authorizing one channel never disturbs another.
   Drop in as many as you like — main.py and gui.py will both pick them up
   automatically and let you choose.

3. pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib requests
4. First upload to a given channel opens a browser once to authorize. After
   that, its token file is cached and reused automatically — you will NOT
   be asked to log in again unless you delete the token file or the
   refresh token is revoked.

SCHEDULING: when upload_pipeline(..., schedule=True) is used, clips are not
all published immediately. Instead each one is uploaded as "private" with a
publishAt timestamp spaced schedule_interval_hours apart (default 2h), so
YouTube auto-publishes them one at a time on a drip schedule instead of
dropping every clip on the channel at once.
"""

import os
import json
import time
import re
import unicodedata
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
import googleapiclient.discovery
import googleapiclient.http
import googleapiclient.errors

# ── Logging (matches step1/2/3 style) ───────────────────────────────────────

def log(msg):      print(f"  {msg}", flush=True)
def log_step(msg): print(f"\n  >>> {msg}", flush=True)
def log_ok(msg):   print(f"  ✅ {msg}", flush=True)
def log_warn(msg): print(f"  ⚠️  {msg}", flush=True)
def log_err(msg):  print(f"  ❌ {msg}", flush=True)

# ── Local AI config (same Ollama instance as step2_clip.py) ────────────────

OLLAMA_URL     = "http://localhost:11434/api/generate"
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL", "llama3.2:1b")  # fast model, plenty for short metadata
OLLAMA_TIMEOUT = 300
MAX_TAGS           = 30   # YouTube tags field (not shown to viewers, used for search/discovery)
MAX_HASHTAGS       = 20   # hashtags appended to the description (viewers see these)
MIN_DESC_WORDS     = 60   # if the LLM gives a too-short description, pad it from the transcript

# ── YouTube API config ───────────────────────────────────────────────────────

SCOPES              = ["https://www.googleapis.com/auth/youtube.upload"]
BASE_DIR            = Path(__file__).resolve().parent

# Legacy single-account env overrides — still respected if you're not using
# the multi-channel naming convention below.
CLIENT_SECRET_FILE  = os.environ.get("YT_CLIENT_SECRET", "client_secret.json")
TOKEN_FILE          = os.environ.get("YT_TOKEN_FILE", "token.json")
DEFAULT_CATEGORY_ID = "22"  # People & Blogs
DEFAULT_PRIVACY     = os.environ.get("YT_PRIVACY", "private")  # private | unlisted | public
DEFAULT_SCHEDULE_INTERVAL_HOURS = 2.0


# ── Multi-channel account discovery ─────────────────────────────────────────

# Matches client_secret.json (Default), client_secret_gaming.json ("Gaming"),
# client_secret-gaming.json ("Gaming"), and client_secret1.json ("1") alike —
# an optional separator (_ or -) is allowed but not required before the suffix.
_CLIENT_SECRET_RE = re.compile(r"^client_secret[_-]?(?P<suffix>[^.]+)?\.json$")


def discover_accounts(base_dir=None) -> list:
    """
    Scan for client_secret*.json files and return one entry per YouTube
    channel:
        [{"label": "Default", "client_secret": "...", "token": "..."}, ...]

    client_secret.json           -> label "Default", token.json
    client_secret_gaming.json    -> label "Gaming",  token_gaming.json
    client_secret1.json          -> label "1",       token_1.json
    """
    base = Path(base_dir) if base_dir else BASE_DIR
    accounts = []
    for f in sorted(base.glob("client_secret*.json")):
        m = _CLIENT_SECRET_RE.match(f.name)
        if not m:
            continue
        suffix = m.group("suffix")
        if suffix:
            label = suffix.replace("_", " ").replace("-", " ").strip().title()
            token_file = base / f"token_{suffix}.json"
        else:
            label = "Default"
            token_file = base / "token.json"
        accounts.append({"label": label, "client_secret": str(f), "token": str(token_file)})
    return accounts


def resolve_account(account: str = None, base_dir=None) -> dict:
    """
    Resolve a requested account (by label, case-insensitive) to its
    {label, client_secret, token} dict. Falls back to the legacy single
    client_secret.json/token.json pair if no client_secret*.json files are
    found at all (keeps old single-channel setups working unmodified).
    """
    accounts = discover_accounts(base_dir)

    if not accounts:
        return {"label": "Default", "client_secret": CLIENT_SECRET_FILE, "token": TOKEN_FILE}

    if account:
        for a in accounts:
            if a["label"].lower() == account.strip().lower():
                return a
        available = ", ".join(a["label"] for a in accounts)
        raise ValueError(f"No channel matching --account '{account}'. Available: {available}")

    if len(accounts) == 1:
        return accounts[0]

    # Multiple accounts and none specified — caller (main.py) is responsible
    # for prompting interactively; if we get here from a non-interactive
    # caller, default to the first one alphabetically rather than crash.
    return accounts[0]


# ── Text sanitization (YouTube rejects raw HTML/SRT tags & control chars) ──

_TAG_RE     = re.compile(r"<[^>]{0,80}>")               # <i>, <c>, <00:00:01.200>, etc.
_CTRL_RE    = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # control chars, keep \n \t
_BLANKS_RE  = re.compile(r"\n{3,}")


def sanitize_text(s: str, max_len: int = 5000) -> str:
    """Strip SRT/HTML-style tags and control characters that make YouTube's API
    reject the upload with 'invalidDescription' / 'invalidTitle', even though
    the JSON itself parses fine."""
    if not s:
        return s
    s = _TAG_RE.sub(" ", s)          # drop <i>, <c.colorXXXXXX>, <00:00:01.200> word-timing tags, etc.
    s = s.replace("<", "").replace(">", "")  # any leftover stray angle brackets
    s = _CTRL_RE.sub("", s)
    s = unicodedata.normalize("NFC", s)
    s = _BLANKS_RE.sub("\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()[:max_len]


def load_filename_map(final_dir: Path) -> dict:
    """step3_overlay.py writes final_metadata.json with the exact clip -> file mapping."""
    meta_path = final_dir / "final_metadata.json"
    if not meta_path.exists():
        log_warn(f"No final_metadata.json in {final_dir} — falling back to clip{{N}}_final.mp4 pattern.")
        return {}
    entries = json.loads(meta_path.read_text())
    return {e["clip"]: final_dir / e["file"] for e in entries}


def get_clip_transcript(srt_entries: list, start_sec: float, end_sec: float) -> str:
    """Slice the real transcript text covering this clip's time range."""
    texts = [e["text"] for e in srt_entries if e["end"] > start_sec and e["start"] < end_sec]
    return " ".join(texts).strip()


def build_prompt(transcript: str, clip_idx: int) -> str:
    context = transcript if transcript else f"(No transcript text found for clip {clip_idx}.)"
    return (
        "You are an expert YouTube Shorts SEO copywriter. Based on the transcript "
        "snippet below, write rich, search-optimized metadata for a short vertical "
        "clip upload. The description must be genuinely long and informative — do "
        "not be lazy or terse.\n\n"
        f'TRANSCRIPT:\n"""{context[:4000]}"""\n\n'
        "Reply ONLY with a JSON object, no markdown fences, no commentary, "
        "in this exact shape:\n"
        "{\n"
        '  "title": "max 90 chars, attention-grabbing hook, no false claims, no hashtags here",\n'
        '  "description": "a DETAILED description of at least 6-8 full sentences '
        f'(roughly {MIN_DESC_WORDS}-150 words). Summarize what happens/what is taught in '
        "the clip, explain why it is worth watching, weave in relevant keywords naturally, "
        "and end with a one-line call-to-action to like/comment/subscribe/follow for more. "
        'Do NOT include any hashtags inside this field.",\n'
        f'  "tags": ["{MAX_TAGS} short lowercase keyword tags for YouTube\'s tag field, no # symbol, '
        'mix of broad and specific terms relevant to the topic"],\n'
        f'  "hashtags": ["{MAX_HASHTAGS} short lowercase hashtag words (no spaces, no # symbol — it '
        'will be added automatically), a mix of broad discovery tags (e.g. shorts, viral, fyp) and '
        'topic-specific tags drawn from the transcript"]\n'
        "}"
    )


def call_ollama(prompt: str) -> str:
    t0 = time.time()
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.5, "num_predict": 900},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        resp.raise_for_status()
        log_ok(f"Ollama responded in {time.time()-t0:.1f}s")
        return resp.json().get("response", "")
    except requests.exceptions.ReadTimeout:
        log_warn(f"Ollama timed out after {time.time()-t0:.0f}s.")
        return ""
    except requests.exceptions.ConnectionError:
        log_err("Cannot reach Ollama at localhost:11434.")
        return ""


def _clean_tag_list(raw_list, limit):
    seen, cleaned = set(), []
    for t in raw_list or []:
        t = sanitize_text(str(t), 50).lstrip("#").lower().replace(" ", "")
        t = re.sub(r"[^a-z0-9_\-]", "", t)
        if t and t not in seen:
            seen.add(t)
            cleaned.append(t)
        if len(cleaned) >= limit:
            break
    return cleaned


def parse_metadata(raw: str, fallback_name: str, transcript: str = "") -> dict:
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start == -1 or end == 0:
        log_warn("No JSON object in Ollama response — using fallback metadata.")
        meta = {"title": fallback_name, "description": fallback_name, "tags": [], "hashtags": []}
    else:
        try:
            data = json.loads(raw[start:end])
            title = sanitize_text(str(data.get("title", "")).strip(), 90) or fallback_name
            description = sanitize_text(str(data.get("description", "")).strip()) or title
            tags = _clean_tag_list(data.get("tags", []), MAX_TAGS)
            hashtags = _clean_tag_list(data.get("hashtags", []), MAX_HASHTAGS)
            meta = {"title": title, "description": description, "tags": tags, "hashtags": hashtags}
        except json.JSONDecodeError:
            log_warn("Could not parse Ollama JSON — using fallback metadata.")
            meta = {"title": fallback_name, "description": fallback_name, "tags": [], "hashtags": []}

    # If the LLM phoned in a short description, pad it with transcript context so the
    # upload still reads as a decent, search-friendly description rather than one line.
    word_count = len(meta["description"].split())
    if word_count < MIN_DESC_WORDS and transcript:
        filler = sanitize_text(transcript.strip())
        if filler:
            meta["description"] = sanitize_text(
                meta["description"].rstrip(". ") + ".\n\n"
                f"In this clip: {filler[:600]}"
            )

    # Always guarantee a baseline set of discovery hashtags even if the LLM gave none.
    baseline_hashtags = ["shorts", "viral", "trending", "fyp", "youtubeshorts"]
    for h in baseline_hashtags:
        if h not in meta["hashtags"] and len(meta["hashtags"]) < MAX_HASHTAGS:
            meta["hashtags"].append(h)

    hashtag_line = " ".join(f"#{h}" for h in meta["hashtags"][:MAX_HASHTAGS])
    if hashtag_line and hashtag_line.lower() not in meta["description"].lower():
        meta["description"] = meta["description"].rstrip() + "\n\n" + hashtag_line

    # Make sure the tags field (separate from on-page hashtags) also has "shorts" for search.
    if "shorts" not in meta["tags"]:
        meta["tags"].append("shorts")
    meta["tags"] = meta["tags"][:MAX_TAGS]

    meta.pop("hashtags", None)  # only needed it to build the description line above
    return meta


def generate_all_metadata(plan: dict, final_dir: Path) -> dict:
    """Returns {file_path: {title, description, tags}} for every rendered clip."""
    srt_entries  = plan.get("srt_entries", [])
    clips        = plan.get("clips", [])
    filename_map = load_filename_map(final_dir)

    log_step(f"Generating metadata for {len(clips)} clip(s) via {OLLAMA_MODEL}")
    results = {}

    for c in clips:
        clip_idx = c["clip"]
        out_file = filename_map.get(clip_idx) or (final_dir / f"clip{clip_idx}_final.mp4")
        if not out_file.exists():
            log_err(f"No rendered file found for clip {clip_idx} — skipping.")
            continue

        transcript = get_clip_transcript(srt_entries, c["start_sec"], c["end_sec"])
        fallback_name = f"Part {clip_idx}"

        log(f"Clip {clip_idx} ({out_file.name}) [{c['start']} → {c['end']}]")
        raw = call_ollama(build_prompt(transcript, clip_idx))
        meta = parse_metadata(raw, fallback_name, transcript)
        log_ok(f'"{meta["title"]}"  ({len(meta["description"].split())} words, {len(meta["tags"])} tags)')

        results[out_file] = meta

    return results


# ── YouTube upload ───────────────────────────────────────────────────────────

def youtube_authenticate(client_secret_file: str = None, token_file: str = None):
    """
    Reuses the given account's cached token file if valid; only opens a
    browser when truly needed. Falls back to the legacy CLIENT_SECRET_FILE /
    TOKEN_FILE module constants if no explicit paths are passed, so existing
    single-channel scripts/imports keep working unmodified.
    """
    client_secret_file = client_secret_file or CLIENT_SECRET_FILE
    token_file = token_file or TOKEN_FILE

    creds = None
    token_path = Path(token_file)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log(f"Refreshing expired YouTube token ({token_path.name})...")
            creds.refresh(Request())
        else:
            if not Path(client_secret_file).exists():
                raise FileNotFoundError(
                    f"Missing {client_secret_file}. Download the OAuth client JSON from "
                    "Google Cloud Console (see step4_upload.py docstring) and place it here."
                )
            log_step(f"Opening browser for one-time YouTube authorization ({Path(client_secret_file).name})...")
            flow = InstalledAppFlow.from_client_secrets_file(client_secret_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        log_ok(f"Token saved to {token_path.name} — future runs won't need to log in again.")

    return googleapiclient.discovery.build("youtube", "v3", credentials=creds)


def upload_video(youtube, file_path: Path, meta: dict, privacy_status: str, publish_at: str = None) -> str:
    safe_title = sanitize_text(meta["title"], 100) or "Untitled"
    safe_description = sanitize_text(meta["description"], 5000)
    safe_tags = [sanitize_text(t, 30) for t in meta["tags"]]
    safe_tags = [t for t in safe_tags if t]

    status = {
        "privacyStatus": privacy_status,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        # YouTube requires privacyStatus "private" for a scheduled (publishAt) upload;
        # it flips to "public" automatically at the scheduled time.
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at

    body = {
        "snippet": {
            "title": safe_title,
            "description": safe_description,
            "tags": safe_tags,
            "categoryId": DEFAULT_CATEGORY_ID,
        },
        "status": status,
    }
    media = googleapiclient.http.MediaFileUpload(
        str(file_path), chunksize=-1, resumable=True, mimetype="video/mp4"
    )
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status_resp, response = request.next_chunk()
        if status_resp:
            log(f"Uploading {file_path.name}: {int(status_resp.progress() * 100)}%")

    video_id = response["id"]
    if publish_at:
        log_ok(f"Uploaded → https://youtu.be/{video_id}  (scheduled for {publish_at})")
    else:
        log_ok(f"Uploaded → https://youtu.be/{video_id}  ({privacy_status})")
    return video_id


# ── Pipeline entry ───────────────────────────────────────────────────────────

def upload_pipeline(
    plan: dict,
    final_dir: str,
    privacy: str = DEFAULT_PRIVACY,
    account: str = None,
    client_secret_file: str = None,
    token_file: str = None,
    schedule: bool = False,
    schedule_interval_hours: float = DEFAULT_SCHEDULE_INTERVAL_HOURS,
) -> dict:
    """
    plan: dict returned by step2_clip.clip_pipeline()
    final_dir: dir path returned by step3_overlay.overlay_pipeline()
    account: channel label to resolve via discover_accounts() (e.g. "Gaming").
             Ignored if client_secret_file/token_file are passed explicitly.
    schedule: if True, clips are NOT all published at once. Each is uploaded
              privately with a publishAt timestamp spaced
              schedule_interval_hours apart (1st clip publishes after one
              interval, 2nd after two intervals, etc.) so YouTube drips them
              out on a schedule instead of dropping everything simultaneously.
    schedule_interval_hours: hours between each scheduled clip (default 2).
    Returns: {file_path_str: youtube_video_id_or_None}
    """
    if not client_secret_file or not token_file:
        acc = resolve_account(account)
        client_secret_file = client_secret_file or acc["client_secret"]
        token_file = token_file or acc["token"]
        channel_label = acc["label"]
    else:
        channel_label = Path(client_secret_file).stem

    print(f"\n[Step 4] Generating metadata + uploading to YouTube (channel: {channel_label})")
    print("  " + "-" * 44)

    final_dir = Path(final_dir)
    metadata = generate_all_metadata(plan, final_dir)
    if not metadata:
        log_err("No clips with metadata to upload.")
        return {}

    youtube = youtube_authenticate(client_secret_file, token_file)

    if schedule:
        log_step(
            f"Scheduling {len(metadata)} clip(s) to '{channel_label}', "
            f"{schedule_interval_hours:g}h apart (first publishes in {schedule_interval_hours:g}h)"
        )
    else:
        log_step(f"Uploading {len(metadata)} clip(s) to '{channel_label}' (privacy={privacy})")

    results = {}
    now = datetime.now(timezone.utc)
    for i, (file_path, meta) in enumerate(metadata.items()):
        try:
            publish_at = None
            if schedule:
                publish_time = now + timedelta(hours=schedule_interval_hours * (i + 1))
                publish_at = publish_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            video_id = upload_video(youtube, file_path, meta, privacy, publish_at=publish_at)
            results[str(file_path)] = video_id
        except googleapiclient.errors.HttpError as e:
            log_err(f"Failed to upload {file_path.name}: {e}")
            results[str(file_path)] = None

    ok = sum(1 for v in results.values() if v)
    print(f"\n[Step 4] Done — {ok}/{len(results)} uploaded")
    return results


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python3 step4_upload.py <video_dir> [account_label] [--schedule] [--interval HOURS]")
        print("  (loads clips_plan.json + RawVideos files, finds matching FinalVideos folder)")
        print("  account_label: e.g. 'Gaming' — matches a client_secret_gaming.json. Omit for Default.")
        sys.exit(1)

    video_dir = sys.argv[1]
    rest = sys.argv[2:]
    schedule_flag = "--schedule" in rest
    interval = DEFAULT_SCHEDULE_INTERVAL_HOURS
    if "--interval" in rest:
        try:
            interval = float(rest[rest.index("--interval") + 1])
        except (IndexError, ValueError):
            pass
    account_arg = None
    for a in rest:
        if a not in ("--schedule", "--interval", str(interval)):
            account_arg = a
            break

    plan_path = os.path.join(video_dir, "clips_plan.json")
    if not os.path.exists(plan_path):
        print(f"❌ clips_plan.json not found in {video_dir}")
        sys.exit(1)

    from step2_clip import parse_srt

    clips = json.load(open(plan_path))
    video_path, srt_entries = "", []
    for f in os.listdir(video_dir):
        if f.endswith(".mp4") and not video_path:
            video_path = os.path.join(video_dir, f)
        if f.endswith(".srt"):
            srt_entries = parse_srt(open(os.path.join(video_dir, f), encoding="utf-8", errors="replace").read())

    final_dir = os.path.join(os.path.dirname(__file__), "FinalVideos", os.path.basename(video_dir))
    fake_plan = {"video_path": video_path, "video_dir": video_dir, "srt_entries": srt_entries, "clips": clips}
    print(json.dumps(
        upload_pipeline(fake_plan, final_dir, account=account_arg, schedule=schedule_flag, schedule_interval_hours=interval),
        indent=2,
    ))