#!/usr/bin/env python3
"""
SortCliper - Step 1: Download YouTube video with audio, captions, metadata, chapters.
"""

import sys
import os
import json
import subprocess

RAWVIDEOS_DIR = os.path.join(os.path.dirname(__file__), "RawVideos")


def download_video(url: str) -> str:
    os.makedirs(RAWVIDEOS_DIR, exist_ok=True)

    video_id = subprocess.check_output(
        ["yt-dlp", "--print", "%(id)s", "--no-download", url]
    ).decode().strip()

    raw_title = subprocess.check_output(
        ["yt-dlp", "--print", "%(title)s", "--no-download", url]
    ).decode().strip()

    safe_title = "".join(c if c.isalnum() or c in " _-" else "_" for c in raw_title).strip()
    folder_name = f"{safe_title}__{video_id}"
    video_dir = os.path.join(RAWVIDEOS_DIR, folder_name)
    os.makedirs(video_dir, exist_ok=True)

    print(f"  📁 Saving to: {video_dir}\n")

    result = subprocess.run([
        "yt-dlp",
        url,
        "-o", os.path.join(video_dir, "%(title)s.%(ext)s"),
        "-f", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--postprocessor-args", "ffmpeg:-c:a aac",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "en",
        "--convert-subs", "srt",
        "--write-info-json",
        "--write-description",
        "--write-thumbnail",
        "--embed-chapters",
        "--newline",
    ])

    if result.returncode != 0:
        print("\n  ❌ Download failed.")
        sys.exit(1)

    # Print summary
    files = sorted(os.listdir(video_dir))
    print("\n  Files saved:")
    for f in files:
        size = os.path.getsize(os.path.join(video_dir, f))
        print(f"    {f}  ({size / (1024*1024):.2f} MB)")

    # Print chapters if present
    for f in files:
        if f.endswith(".info.json"):
            with open(os.path.join(video_dir, f)) as fh:
                data = json.load(fh)
            chapters = data.get("chapters") or []
            if chapters:
                print(f"\n  📌 Chapters ({len(chapters)}):")
                for ch in chapters:
                    m, s = divmod(int(ch.get("start_time", 0)), 60)
                    print(f"    {m:02d}:{s:02d}  {ch.get('title', '—')}")
            else:
                print("\n  ⚠️  No chapters found for this video.")
            break

    return video_dir


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 step1_download.py <youtube_url>")
        sys.exit(1)
    download_video(sys.argv[1])