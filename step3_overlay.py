#!/usr/bin/env python3
"""
SortCliper - Step 3: Single-pass render.
For each clip plan entry, ONE ffmpeg call does:
  - Seek to start_sec, trim to duration
  - Letterbox to 9:16 (1080x1920)
  - Draw "Part-N" label at the top
  - Burn in captions from the SRT at the bottom
Output: FinalVideos/<folder>/clip1_final.mp4, clip2_final.mp4, …
"""

import os
import sys
import json
import re
import subprocess
import time

FINAL_DIR = os.path.join(os.path.dirname(__file__), "FinalVideos")

# ── Visual constants (tuned for 1080×1920, 9:16) ────────────────────────────
# Part-N label at the top
LABEL_FONT_SIZE  = 52        # ~4.8% of frame height — readable but not huge
LABEL_Y          = 60        # px from top edge
LABEL_BORDER_W   = 3

# Captions are always 4pt larger than the Part-N label.
CAPTION_FONT_SIZE = LABEL_FONT_SIZE + 4   # → 56
CAPTION_MARGIN_V  = 80       # px from bottom — keeps text inside the safe zone
CAPTION_BORDER_W  = 2
# Wrap captions at this many characters so long lines don't overflow 1080px width.
# At 28pt, ~1 char ≈ 18px → 52 chars ≈ 936px (within 1080px with margins).
CAPTION_MAX_CHARS = 52


def log(msg):      print(f"  {msg}", flush=True)
def log_step(msg): print(f"\n  >>> {msg}", flush=True)
def log_ok(msg):   print(f"  ✅ {msg}", flush=True)
def log_warn(msg): print(f"  ⚠️  {msg}", flush=True)
def log_err(msg):  print(f"  ❌ {msg}", flush=True)


# ── SRT helpers ──────────────────────────────────────────────────────────────

def ts_to_sec(t: str) -> float:
    t = t.strip().replace(",", ".")
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def sec_to_srt(sec: float) -> str:
    h  = int(sec // 3600)
    m  = int((sec % 3600) // 60)
    s  = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clip_srt_entries(entries: list, clip_start: float, clip_end: float) -> list:
    """Keep only entries overlapping [clip_start, clip_end], re-zero the timestamps."""
    out = []
    for e in entries:
        if e["end"] <= clip_start or e["start"] >= clip_end:
            continue
        out.append({
            "start": max(0.0, e["start"] - clip_start),
            "end":   min(clip_end - clip_start, e["end"] - clip_start),
            "text":  e["text"],
        })
    return out


def write_tmp_srt(entries: list, path: str):
    with open(path, "w", encoding="utf-8") as fh:
        for i, e in enumerate(entries, 1):
            fh.write(f"{i}\n")
            fh.write(f"{sec_to_srt(e['start'])} --> {sec_to_srt(e['end'])}\n")
            fh.write(f"{e['text']}\n\n")


# ── ffmpeg filter helpers ────────────────────────────────────────────────────

def build_vf_filter(width: int, height: int) -> str:
    """Scale + pad source to 1080x1920 (9:16)."""
    if width / height < 1.0:
        # Already portrait — just scale up
        return "scale=1080:1920:flags=lanczos,fps=18"
    else:
        # Landscape — pillarbox with black bars
        return "scale=1080:-2:flags=lanczos,pad=1080:1920:0:(1920-ih)/2:black,fps=18"


def esc(s: str) -> str:
    """Escape a value for ffmpeg drawtext."""
    return (
        s.replace("\\", "\\\\")
         .replace("'",  "\\'")
         .replace(":",  "\\:")
         .replace(",",  "\\,")
    )


def build_vf_with_overlays(
    width: int, height: int, clip_num: int, tmp_srt: str, has_srt: bool
) -> str:
    """
    Full video filter chain:
      1. Scale / letterbox to 9:16
      2. drawtext — Part-N label at top
      3. subtitles  — burned-in captions at bottom  (only if SRT exists)
    """
    # Step 1: geometry
    vf = build_vf_filter(width, height)

    # Step 2: Part-N label — top center, safe inside frame
    label = f"Part-{clip_num}"
    vf += (
        f",drawtext="
        f"text='{esc(label)}':"
        f"fontsize={LABEL_FONT_SIZE}:"
        f"fontcolor=white:"
        f"bordercolor=black:"
        f"borderw={LABEL_BORDER_W}:"
        f"font='DejaVu Sans Bold':"
        f"x=(w-text_w)/2:"
        f"y={LABEL_Y}"
    )

    # Step 3: captions
    if has_srt:
        srt_path = tmp_srt.replace("\\", "/").replace(":", "\\:")
        # PlayResX/Y anchor font sizes to the actual frame dimensions.
        # Without them libass guesses and the result is unpredictably large.
        # WrapStyle=0 = smart wrap (breaks at spaces, never overflows width).
        # MarginL/R add horizontal padding so wrapped lines don't touch the edge.
        sub_style = (
            f"PlayResX=1080,"
            f"PlayResY=1920,"
            f"FontSize={CAPTION_FONT_SIZE},"
            f"PrimaryColour=&H00FFFFFF,"
            f"OutlineColour=&H00000000,"
            f"Outline={CAPTION_BORDER_W},"
            f"Shadow=0,"
            f"Bold=1,"
            f"Alignment=2,"          # bottom-center (ASS numpad)
            f"MarginL=40,"
            f"MarginR=40,"
            f"MarginV={CAPTION_MARGIN_V},"
            f"WrapStyle=0"
        )
        vf += f",subtitles='{srt_path}':force_style='{sub_style}'"

    return vf


# ── Single clip render ───────────────────────────────────────────────────────

def render_clip(
    video_path: str,
    out_path:   str,
    clip:       dict,
    vf:         str,
) -> bool:
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(clip["start_sec"]),          # fast seek BEFORE -i
        "-i", video_path,
        "-t", str(clip["duration"]),
        "-avoid_negative_ts", "make_zero",
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-preset", "fast",
        "-crf", "23",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log_err(f"ffmpeg failed for clip {clip['clip']}:")
        print(result.stderr.decode()[-600:], flush=True)
        return False
    return True


# ── Pipeline entry ───────────────────────────────────────────────────────────

def overlay_pipeline(plan: dict) -> str:
    """
    plan is the dict returned by step2_clip.clip_pipeline():
      { video_path, video_dir, srt_entries, clips, width, height }
    """
    print("\n[Step 3] Single-pass render: cut + letterbox + label + captions")
    print("  " + "-" * 44)

    video_path  = plan["video_path"]
    video_dir   = plan["video_dir"]
    srt_entries = plan["srt_entries"]
    clips       = plan["clips"]
    width       = plan["width"]
    height      = plan["height"]

    folder_name = os.path.basename(video_dir)
    out_dir     = os.path.join(FINAL_DIR, folder_name)
    os.makedirs(out_dir, exist_ok=True)
    log_ok(f"Output folder: {out_dir}")
    log_ok(f"Source: {width}x{height}  →  1080x1920 (9:16)")
    log_ok(f"Clips to render: {len(clips)}")

    tmp_srt  = os.path.join(out_dir, "_tmp.srt")
    metadata = []
    total    = len(clips)

    for i, clip in enumerate(clips, 1):
        idx       = clip["clip"]
        out_path  = os.path.join(out_dir, f"clip{idx}_final.mp4")

        log_step(
            f"Rendering {i}/{total}: clip{idx}  "
            f"{clip['start']} → {clip['end']}  ({clip['duration']:.0f}s)"
        )

        # Slice SRT to this clip's window and write temp file
        sub_entries = clip_srt_entries(srt_entries, clip["start_sec"], clip["end_sec"])
        has_srt = bool(sub_entries)
        if has_srt:
            write_tmp_srt(sub_entries, tmp_srt)
            log(f"Captions in window: {len(sub_entries)}")
        else:
            log_warn("No captions in this window — skipping subtitle overlay.")

        vf = build_vf_with_overlays(width, height, idx, tmp_srt, has_srt)

        t0 = time.time()
        ok = render_clip(video_path, out_path, clip, vf)
        elapsed = time.time() - t0

        if ok:
            size = os.path.getsize(out_path) / (1024 * 1024)
            log_ok(f"clip{idx}_final.mp4 — {size:.1f} MB in {elapsed:.1f}s")
            metadata.append({
                "clip":             idx,
                "file":             f"clip{idx}_final.mp4",
                "start":            clip["start"],
                "end":              clip["end"],
                "duration_seconds": round(clip["duration"]),
            })

    # Clean up temp SRT
    if os.path.exists(tmp_srt):
        os.remove(tmp_srt)

    meta_path = os.path.join(out_dir, "final_metadata.json")
    with open(meta_path, "w") as fh:
        json.dump(metadata, fh, indent=2)
    log_ok(f"final_metadata.json saved ({len(metadata)} clips)")

    print(f"\n[Step 3] Done → {out_dir}")
    return out_dir


if __name__ == "__main__":
    # Standalone use: python3 step3_overlay.py <video_dir>
    # Loads clips_plan.json from that directory.
    if len(sys.argv) < 2:
        print("Usage: python3 step3_overlay.py <video_dir>")
        sys.exit(1)

    video_dir  = sys.argv[1]
    plan_path  = os.path.join(video_dir, "clips_plan.json")
    if not os.path.exists(plan_path):
        print(f"❌  clips_plan.json not found in {video_dir}")
        print("    Run step2_clip.py first.")
        sys.exit(1)

    clips = json.load(open(plan_path))

    # Locate video + SRT
    video_path = ""
    srt_entries = []
    for f in os.listdir(video_dir):
        if f.endswith(".mp4") and not video_path:
            video_path = os.path.join(video_dir, f)
        if f.endswith(".srt"):
            from step2_clip import parse_srt
            srt_entries = parse_srt(
                open(os.path.join(video_dir, f), encoding="utf-8", errors="replace").read()
            )

    result = subprocess.check_output([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", video_path
    ])
    stream = json.loads(result)["streams"][0]

    overlay_pipeline({
        "video_path":  video_path,
        "video_dir":   video_dir,
        "srt_entries": srt_entries,
        "clips":       clips,
        "width":       int(stream["width"]),
        "height":      int(stream["height"]),
    })