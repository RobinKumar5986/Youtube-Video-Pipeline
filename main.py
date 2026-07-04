#!/usr/bin/env python3
"""
SortCliper - Main Pipeline Controller

Usage:
  python3 main.py <youtube_url> [--privacy private|unlisted|public] [--account NAME] [--no-upload]
                   [--no-ai-clip] [--schedule] [--schedule-interval HOURS]
      → runs the full pipeline (download → clip → render → upload)

  python3 main.py 1 <youtube_url>
      → Step 1 only: download

  python3 main.py 2 [--no-ai-clip]
      → Step 2 only: plan clips for an existing RawVideos/ folder

  python3 main.py 3
      → Step 3 only: render an existing clip plan

  python3 main.py 4 [--privacy private|unlisted|public] [--account NAME] [--schedule] [--schedule-interval HOURS]
      → Step 4 only: generate metadata + upload already-rendered clips

  Steps 2-4 prompt you to pick a folder if more than one exists in
  RawVideos/. If there's only one, it's used automatically — no prompt.

  --account NAME selects which YouTube channel to upload to, matching a
  client_secret_NAME.json (or client_secretNAME.json) file (see
  step4_upload.py docstring for the multi-channel naming convention). If you
  only have one client_secret*.json in the project, it's used automatically
  — no prompt. If you have several and don't pass --account, you'll be
  prompted to pick one.

  --no-ai-clip disables the Ollama-based clip planning in Step 2 (no ad/
  intro/outro detection, no sentence-boundary snapping). Instead the entire
  video is mechanically cut into fixed ~1-minute clips.

  --schedule uploads clips on a drip schedule instead of publishing them all
  at once: clips are uploaded privately with a publishAt timestamp spaced
  --schedule-interval hours apart (default: 2h).
"""

import os
import sys
import json
import subprocess
import argparse

from step1_download import download_video
from step2_clip     import clip_pipeline, parse_srt
from step3_overlay  import overlay_pipeline
from step4_upload    import upload_pipeline, discover_accounts

BASE_DIR  = os.path.dirname(__file__)
RAW_DIR   = os.path.join(BASE_DIR, "RawVideos")
FINAL_DIR = os.path.join(BASE_DIR, "FinalVideos")


# ── Folder selection ─────────────────────────────────────────────────────────

def select_video_dir() -> str:
    """List RawVideos/ folders, auto-pick if only one, else prompt by number."""
    if not os.path.isdir(RAW_DIR):
        print(f"❌ {RAW_DIR} doesn't exist yet. Run step 1 first.")
        sys.exit(1)

    folders = sorted(f for f in os.listdir(RAW_DIR) if os.path.isdir(os.path.join(RAW_DIR, f)))
    if not folders:
        print(f"❌ No video folders found in {RAW_DIR}. Run step 1 first.")
        sys.exit(1)

    if len(folders) == 1:
        print(f"  📁 Using folder: {folders[0]} (only one found)")
        return os.path.join(RAW_DIR, folders[0])

    print("\n  Available video folders:")
    for i, f in enumerate(folders, 1):
        print(f"    [{i}] {f}")
    choice = input("\n  Select a folder number: ").strip()

    try:
        idx = int(choice)
        if not (1 <= idx <= len(folders)):
            raise ValueError
    except ValueError:
        print("  ❌ Invalid selection.")
        sys.exit(1)

    return os.path.join(RAW_DIR, folders[idx - 1])


# ── YouTube channel/account selection ───────────────────────────────────────

def select_account(account_arg: str = None) -> dict:
    """
    Resolve which YouTube channel (client_secret*.json) to upload with.
    Auto-picks if there's exactly one, matches --account by label if given,
    otherwise prompts by number — same UX as select_video_dir().
    """
    accounts = discover_accounts(BASE_DIR)

    if not accounts:
        print("  ⚠️  No client_secret*.json found — falling back to legacy client_secret.json/token.json.")
        return {"label": "Default", "client_secret": "client_secret.json", "token": "token.json"}

    if len(accounts) == 1:
        acc = accounts[0]
        print(f"  📺 Using channel: {acc['label']} (only one found)")
        return acc

    if account_arg:
        for acc in accounts:
            if acc["label"].lower() == account_arg.strip().lower():
                print(f"  📺 Using channel: {acc['label']}")
                return acc
        print(f"  ❌ No channel matching --account '{account_arg}'.")
        print("  Available channels: " + ", ".join(a["label"] for a in accounts))
        sys.exit(1)

    print("\n  Available YouTube channels:")
    for i, a in enumerate(accounts, 1):
        print(f"    [{i}] {a['label']}")
    choice = input("\n  Select a channel number: ").strip()

    try:
        idx = int(choice)
        if not (1 <= idx <= len(accounts)):
            raise ValueError
    except ValueError:
        print("  ❌ Invalid selection.")
        sys.exit(1)

    return accounts[idx - 1]


# ── Rebuild plan dict from disk (for resuming at step 3 or 4) ──────────────

def load_plan_from_dir(video_dir: str) -> dict:
    plan_path = os.path.join(video_dir, "clips_plan.json")
    if not os.path.exists(plan_path):
        print(f"❌ clips_plan.json not found in {video_dir}. Run step 2 first.")
        sys.exit(1)
    clips = json.load(open(plan_path))

    video_path, srt_entries = "", []
    for f in os.listdir(video_dir):
        if f.endswith(".mp4") and not video_path:
            video_path = os.path.join(video_dir, f)
        if f.endswith(".srt"):
            srt_entries = parse_srt(
                open(os.path.join(video_dir, f), encoding="utf-8", errors="replace").read()
            )

    if not video_path:
        print(f"❌ No mp4 found in {video_dir}")
        sys.exit(1)

    result = subprocess.check_output([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", video_path
    ])
    stream = json.loads(result)["streams"][0]

    return {
        "video_path":  video_path,
        "video_dir":   video_dir,
        "srt_entries": srt_entries,
        "clips":       clips,
        "width":       int(stream["width"]),
        "height":      int(stream["height"]),
    }


# ── Full pipeline ─────────────────────────────────────────────────────────────

def run_pipeline(
    url: str,
    privacy: str = "private",
    do_upload: bool = True,
    account: str = None,
    use_ai: bool = True,
    schedule: bool = False,
    schedule_interval: float = 2.0,
):
    print("=" * 50)
    print("         SortCliper Pipeline")
    print("=" * 50)

    print("\n[Step 1] Downloading video...")
    video_dir = download_video(url)
    print(f"[Step 1] Done → {video_dir}")

    plan = clip_pipeline(video_dir, use_ai=use_ai)
    print(f"[Step 2] Done — {len(plan['clips'])} clips planned")

    final_dir = overlay_pipeline(plan)
    print(f"[Step 3] Done → {final_dir}")

    if not do_upload:
        print("\n[Step 4] Skipped (--no-upload set)")
        print_summary(final_dir, uploaded=None)
        return

    acc = select_account(account)
    uploaded = upload_pipeline(
        plan, final_dir, privacy=privacy,
        client_secret_file=acc["client_secret"], token_file=acc["token"],
        schedule=schedule, schedule_interval_hours=schedule_interval,
    )
    print_summary(final_dir, uploaded)


# ── Single-step runner ──────────────────────────────────────────────────────

def run_step(
    step: int,
    url: str = None,
    privacy: str = "private",
    account: str = None,
    use_ai: bool = True,
    schedule: bool = False,
    schedule_interval: float = 2.0,
):
    if step == 1:
        if not url:
            print("Step 1 needs a URL: python3 main.py 1 <youtube_url>")
            sys.exit(1)
        video_dir = download_video(url)
        print(f"[Step 1] Done → {video_dir}")
        return

    video_dir = select_video_dir()

    if step == 2:
        plan = clip_pipeline(video_dir, use_ai=use_ai)
        print(f"[Step 2] Done — {len(plan['clips'])} clips planned")
        return

    if step == 3:
        plan = load_plan_from_dir(video_dir)
        final_dir = overlay_pipeline(plan)
        print(f"[Step 3] Done → {final_dir}")
        return

    if step == 4:
        plan = load_plan_from_dir(video_dir)
        final_dir = os.path.join(FINAL_DIR, os.path.basename(video_dir))
        if not os.path.isdir(final_dir):
            print(f"❌ No rendered clips at {final_dir}. Run step 3 first.")
            sys.exit(1)
        acc = select_account(account)
        uploaded = upload_pipeline(
            plan, final_dir, privacy=privacy,
            client_secret_file=acc["client_secret"], token_file=acc["token"],
            schedule=schedule, schedule_interval_hours=schedule_interval,
        )
        print_summary(final_dir, uploaded)
        return


def print_summary(final_dir, uploaded):
    print("\n" + "=" * 50)
    print("  ✅ Pipeline complete!")
    print(f"  📂 Final clips → {final_dir}")
    if uploaded:
        for path, vid in uploaded.items():
            link = f"https://youtu.be/{vid}" if vid else "FAILED"
            print(f"     • {path} → {link}")
    print("=" * 50)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SortCliper pipeline")
    parser.add_argument("target", help="YouTube URL (full pipeline) OR step number 1-4")
    parser.add_argument("extra_url", nargs="?", help="YouTube URL — only used with target=1")
    parser.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    parser.add_argument("--no-upload", action="store_true", help="Full pipeline only: skip step 4")
    parser.add_argument(
        "--account",
        help="YouTube channel label to upload to (matches client_secret_<NAME>.json or "
             "client_secret<NAME>.json). Omit if you only have one client_secret*.json, "
             "or to be prompted when there are several."
    )
    parser.add_argument(
        "--no-ai-clip", action="store_true",
        help="Disable AI-based clip planning (Step 2); just cut the whole video into fixed 1-minute clips."
    )
    parser.add_argument(
        "--schedule", action="store_true",
        help="Schedule uploads on a drip (spaced by --schedule-interval) instead of publishing all at once."
    )
    parser.add_argument(
        "--schedule-interval", type=float, default=2.0,
        help="Hours between each scheduled upload when --schedule is set (default: 2)."
    )
    args = parser.parse_args()

    use_ai = not args.no_ai_clip

    if args.target.isdigit():
        step = int(args.target)
        if step not in (1, 2, 3, 4):
            print("Step must be 1, 2, 3, or 4")
            sys.exit(1)
        run_step(
            step, url=args.extra_url, privacy=args.privacy, account=args.account,
            use_ai=use_ai, schedule=args.schedule, schedule_interval=args.schedule_interval,
        )
    else:
        run_pipeline(
            args.target, privacy=args.privacy, do_upload=not args.no_upload, account=args.account,
            use_ai=use_ai, schedule=args.schedule, schedule_interval=args.schedule_interval,
        )