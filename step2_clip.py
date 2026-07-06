#!/usr/bin/env python3
"""
SortCliper - Step 2: Compute clip ranges (NO video rendering).
- Parses SRT and chapters
- If AI clipping is enabled: asks Ollama to identify skip ranges (ads,
  intros, outros), then snaps cut points to sentence boundaries.
- If AI clipping is disabled: skips all of that and just cuts the entire
  video into fixed-length clips, no analysis, no Ollama call.
- Returns plan dict for Step 3; also saves clips_plan.json in video_dir
"""

import os
import sys
import json
import subprocess
import requests
import time
import re

OLLAMA_URL    = "http://localhost:11434/api/generate"

CLIP_MIN_SEC  = 60
CLIP_MAX_SEC  = 150   # 2m30s max — default cap, overridable via max_clip_length
OLLAMA_TIMEOUT = 600

FIXED_CLIP_SEC     = 60   # default length used when AI clipping is disabled
FIXED_CLIP_MIN_TAIL = 10  # drop a trailing fragment shorter than this (scaled down for short clip lengths)

MODELS = {
    "1": {"name": "llama3:latest",  "label": "llama3 8B (slower, smarter)"},
    "2": {"name": "llama3.2:1b",    "label": "llama3.2 1B (faster, lighter)"},
}

AD_KEYWORDS = [
    "sponsor", "sponsored", "ad ", "promo", "promotion",
    "subscribe", "intro", "outro", "untitled", "support",
    "merch", "patreon", "channel", "discount", "coupon",
    "use code", "check out", "brought to you",
]


def log(msg):      print(f"  {msg}", flush=True)
def log_step(msg): print(f"\n  >>> {msg}", flush=True)
def log_ok(msg):   print(f"  ✅ {msg}", flush=True)
def log_warn(msg): print(f"  ⚠️  {msg}", flush=True)
def log_err(msg):  print(f"  ❌ {msg}", flush=True)


def choose_model() -> str:
    """Interactive terminal picker — only used when no model is passed in
    explicitly (e.g. running main.py without --model from a real terminal)."""
    import threading

    print("\n  Choose Ollama model for clip analysis:")
    for key, val in MODELS.items():
        print(f"    [{key}] {val['label']}")
    print("\n  Enter 1 or 2 (default 1, auto-selects in 15s): ", end="", flush=True)

    choice = [None]

    def _read():
        try:
            choice[0] = input().strip()
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=15)

    if not choice[0]:
        print("\n  (no input — using default)")
        choice[0] = "1"

    model = MODELS.get(choice[0], MODELS["1"])
    log_ok(f"Model selected: {model['label']}")
    return model["name"]


def load_srt(video_dir: str) -> str:
    log_step("Loading captions (SRT)...")
    for f in os.listdir(video_dir):
        if f.endswith(".srt"):
            path = os.path.join(video_dir, f)
            content = open(path, encoding="utf-8", errors="replace").read()
            log_ok(f"Loaded: {f} ({len(content)} chars)")
            return content
    log_warn("No SRT file found.")
    return ""


def parse_srt(srt_text: str) -> list:
    """Parse SRT text into list of {start, end, text} in seconds."""
    entries = []
    for block in re.split(r"\n\n+", srt_text.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        try:
            arrow = lines[1].split(" --> ")
            if len(arrow) != 2:
                continue
            def ts(t):
                t = t.strip().replace(",", ".")
                h, m, s = t.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            entries.append({
                "start": ts(arrow[0]),
                "end":   ts(arrow[1]),
                "text":  " ".join(lines[2:]).strip(),
            })
        except Exception:
            continue
    return entries


def load_chapters(video_dir: str) -> list:
    log_step("Loading chapters...")
    for f in os.listdir(video_dir):
        if f.endswith(".info.json"):
            data = json.load(open(os.path.join(video_dir, f)))
            chapters = data.get("chapters") or []
            if chapters:
                log_ok(f"Found {len(chapters)} chapters:")
                for ch in chapters:
                    m, s   = divmod(int(ch.get("start_time", 0)), 60)
                    em, es = divmod(int(ch.get("end_time", 0)), 60)
                    log(f"    {m:02d}:{s:02d} - {em:02d}:{es:02d}  {ch.get('title', '')}")
            else:
                log_warn("No chapters found.")
            return chapters
    return []


def load_video_file(video_dir: str) -> str:
    log_step("Locating video file...")
    for f in os.listdir(video_dir):
        if f.endswith(".mp4"):
            path = os.path.join(video_dir, f)
            size = os.path.getsize(path) / (1024 * 1024)
            log_ok(f"Found: {f} ({size:.1f} MB)")
            return path
    log_err("No mp4 found.")
    return ""


def get_video_duration(video_path: str) -> float:
    log_step("Reading video duration...")
    result = subprocess.check_output([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", video_path
    ])
    duration = float(json.loads(result)["format"]["duration"])
    m, s = divmod(int(duration), 60)
    log_ok(f"Duration: {m:02d}:{s:02d}")
    return duration


def get_video_dimensions(video_path: str) -> tuple:
    result = subprocess.check_output([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-select_streams", "v:0", video_path
    ])
    stream = json.loads(result)["streams"][0]
    return int(stream["width"]), int(stream["height"])


# ── Skip range detection (AI mode only) ──────────────────────────────────────

def skip_ranges_from_chapters(chapters: list) -> list:
    """Heuristic fallback: flag junk-titled chapters as skip zones."""
    skips = []
    for ch in chapters:
        title = ch.get("title", "").lower()
        if any(kw in title for kw in AD_KEYWORDS):
            skips.append({
                "start":  ch.get("start_time", 0),
                "end":    ch.get("end_time", 0),
                "reason": f"chapter: {ch.get('title', '')}",
            })
    return skips


def ask_ollama_for_skip_ranges(
    model: str, srt_entries: list, chapters: list, duration: float
) -> list:
    chapters_text = ""
    if chapters:
        parts = []
        for ch in chapters:
            m, s   = divmod(int(ch.get("start_time", 0)), 60)
            em, es = divmod(int(ch.get("end_time", 0)), 60)
            parts.append(f"{m:02d}:{s:02d}-{em:02d}:{es:02d} {ch.get('title','')}")
        chapters_text = "CHAPTERS: " + " | ".join(parts)

    # Only first 80 lines to keep the prompt small
    transcript = "\n".join(
        f"[{int(e['start'])//60:02d}:{int(e['start'])%60:02d}] {e['text']}"
        for e in srt_entries[:80]
    )

    prompt = (
        f"Identify segments to SKIP in a YouTube video "
        f"(sponsors, ads, intros, outros, subscribe reminders).\n\n"
        f"Duration: {int(duration//60)}:{int(duration%60):02d}\n"
        f"{chapters_text}\n\n"
        f"TRANSCRIPT SAMPLE:\n{transcript}\n\n"
        f"Reply ONLY with a JSON array. Numbers not strings. "
        f'Example: [{{"start":10,"end":45,"reason":"intro"}}]\n'
        f"If nothing to skip, reply: []"
    )

    log_step("Asking Ollama to identify skip ranges...")
    log(f"Timeout: {OLLAMA_TIMEOUT}s  Model: {model}")

    t0 = time.time()
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 256},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        log_ok(f"Ollama responded in {time.time()-t0:.1f}s")
    except requests.exceptions.ReadTimeout:
        log_warn(f"Ollama timed out after {time.time()-t0:.0f}s — using chapter heuristics.")
        return skip_ranges_from_chapters(chapters)
    except requests.exceptions.ConnectionError:
        log_err("Cannot reach Ollama at localhost:11434 — using chapter heuristics.")
        return skip_ranges_from_chapters(chapters)

    raw   = resp.json().get("response", "")
    start = raw.find("[")
    end   = raw.rfind("]") + 1
    if start == -1 or end == 0:
        log_warn("No JSON array in Ollama response — using chapter heuristics.")
        return skip_ranges_from_chapters(chapters)

    try:
        skips = json.loads(raw[start:end])
        if skips:
            log_ok(f"Skip ranges identified ({len(skips)}):")
            for sk in skips:
                sm, ss = divmod(int(sk["start"]), 60)
                em, es = divmod(int(sk["end"]), 60)
                log(f"    {sm:02d}:{ss:02d} → {em:02d}:{es:02d}  [{sk.get('reason','')}]")
        else:
            log_ok("No skip ranges — full video is usable.")
        return skips
    except json.JSONDecodeError:
        log_warn("Could not parse Ollama JSON — using chapter heuristics.")
        return skip_ranges_from_chapters(chapters)


# ── Clip planning (AI mode) ─────────────────────────────────────────────────

def find_sentence_boundary(srt_entries: list, target: float, window: float = 10.0) -> float:
    """Snap target time to nearest sentence-ending subtitle boundary."""
    best, best_dist = target, window + 1

    for e in srt_entries:
        if e["end"] >= target - window and e["end"] <= target + window:
            if e["text"].strip()[-1:] in ".?!":
                d = abs(e["end"] - target)
                if d < best_dist:
                    best_dist, best = d, e["end"]

    if best_dist > window:
        for e in srt_entries:
            if e["end"] >= target - window and e["end"] <= target + window:
                d = abs(e["end"] - target)
                if d < best_dist:
                    best_dist, best = d, e["end"]

    return best


def build_clip_plan(
    srt_entries: list, skip_ranges: list, duration: float,
    max_clip_sec: float = CLIP_MAX_SEC, min_clip_sec: float = CLIP_MIN_SEC,
) -> list:
    """
    Returns a list of clip dicts:
      { clip, start_sec, end_sec, start, end, duration }
    No video is touched here — pure time math.
    max_clip_sec/min_clip_sec let the caller (main.py / gui.py slider)
    control how long each Short is allowed to be.
    """
    log_step(f"Building clip plan (AI-assisted, {min_clip_sec:.0f}-{max_clip_sec:.0f}s per clip)...")

    skip_sorted = sorted(skip_ranges, key=lambda x: x["start"])
    usable, cursor = [], 0.0
    for sk in skip_sorted:
        if cursor < sk["start"]:
            usable.append((cursor, sk["start"]))
        cursor = max(cursor, sk["end"])
    if cursor < duration:
        usable.append((cursor, duration))

    log_ok(f"Usable ranges: {len(usable)}")
    for u in usable:
        m1, s1 = divmod(int(u[0]), 60)
        m2, s2 = divmod(int(u[1]), 60)
        log(f"    {m1:02d}:{s1:02d} → {m2:02d}:{s2:02d} ({u[1]-u[0]:.0f}s)")

    clips, idx = [], 1
    for (rstart, rend) in usable:
        if (rend - rstart) < min_clip_sec:
            log_warn(f"Range too short ({rend-rstart:.0f}s) — skipping.")
            continue
        pos = rstart
        while pos < rend:
            if (rend - pos) < min_clip_sec:
                break
            target_end  = min(pos + max_clip_sec, rend)
            snapped_end = find_sentence_boundary(srt_entries, target_end, window=10.0)
            snapped_end = min(max(snapped_end, pos + min_clip_sec), rend)
            dur = snapped_end - pos
            if dur < min_clip_sec:
                break

            m1, s1 = divmod(int(pos), 60)
            m2, s2 = divmod(int(snapped_end), 60)
            log_ok(f"Clip {idx}: {m1:02d}:{s1:02d} → {m2:02d}:{s2:02d} ({dur:.0f}s)")
            clips.append({
                "clip":      idx,
                "start_sec": pos,
                "end_sec":   snapped_end,
                "start":     f"{m1:02d}:{s1:02d}",
                "end":       f"{m2:02d}:{s2:02d}",
                "duration":  dur,
            })
            idx += 1
            pos = snapped_end

    log_ok(f"Total clips planned: {len(clips)}")
    return clips


# ── Clip planning (non-AI mode) ─────────────────────────────────────────────

def build_fixed_clip_plan(duration: float, clip_length: float = FIXED_CLIP_SEC) -> list:
    """
    AI clipping disabled: no skip-range analysis, no Ollama call, no sentence
    snapping. Just mechanically slices the entire video into fixed-length
    (default 1 minute) clips back-to-back. clip_length is fully controlled
    by the caller (main.py --max-length / the gui.py slider).
    """
    min_tail = min(FIXED_CLIP_MIN_TAIL, max(clip_length * 0.3, 1))
    log_step(f"Building fixed {clip_length:.0f}s clip plan (AI clipping disabled)...")

    clips, idx, pos = [], 1, 0.0
    while pos < duration:
        end = min(pos + clip_length, duration)
        dur = end - pos
        if dur < min_tail:
            log_warn(f"Trailing {dur:.0f}s fragment too short — dropping.")
            break

        m1, s1 = divmod(int(pos), 60)
        m2, s2 = divmod(int(end), 60)
        log_ok(f"Clip {idx}: {m1:02d}:{s1:02d} → {m2:02d}:{s2:02d} ({dur:.0f}s)")
        clips.append({
            "clip":      idx,
            "start_sec": pos,
            "end_sec":   end,
            "start":     f"{m1:02d}:{s1:02d}",
            "end":       f"{m2:02d}:{s2:02d}",
            "duration":  dur,
        })
        idx += 1
        pos = end

    log_ok(f"Total clips planned: {len(clips)}")
    return clips


# ── Pipeline entry ───────────────────────────────────────────────────────────

def clip_pipeline(
    video_dir: str, use_ai: bool = True,
    max_clip_length: float = None, model: str = None,
) -> dict:
    """
    Analyses the video and returns everything Step 3 needs to render in one pass:
      {
        video_path  : str             — source .mp4
        video_dir   : str             — raw video folder (for SRT path)
        srt_entries : list            — all parsed subtitle dicts
        clips       : list            — clip plan dicts
        width       : int
        height      : int
      }
    Writing clips_plan.json to video_dir is the only side-effect.

    use_ai=True  (default): Ollama identifies ad/intro/outro skip ranges,
                  cut points are snapped to sentence boundaries (original behavior).
    use_ai=False: skips model selection and the Ollama call entirely — the
                  whole video is just chopped into fixed-length clips.
    max_clip_length: caps how long each clip is allowed to be (AI mode), or
                  sets the exact clip length (non-AI mode). Falls back to
                  CLIP_MAX_SEC / FIXED_CLIP_SEC if not given.
    model: Ollama model name to use for skip-range analysis. If not given,
                  falls back to the interactive terminal picker.
    """
    print("\n[Step 2] Planning clips (no rendering)")
    print("  " + "-" * 44)
    print(f"  Mode: {'AI-assisted clipping' if use_ai else 'Fixed-length clips (AI clipping disabled)'}")

    video_path  = load_video_file(video_dir)
    if not video_path:
        sys.exit(1)

    srt_text    = load_srt(video_dir)
    srt_entries = parse_srt(srt_text) if srt_text else []
    log_ok(f"Parsed {len(srt_entries)} subtitle entries")

    duration = get_video_duration(video_path)

    log_step("Detecting source dimensions...")
    width, height = get_video_dimensions(video_path)
    log_ok(f"Source: {width}x{height}")

    effective_max = max_clip_length if max_clip_length else (CLIP_MAX_SEC if use_ai else FIXED_CLIP_SEC)
    effective_min = CLIP_MIN_SEC if effective_max >= CLIP_MIN_SEC else max(10.0, effective_max * 0.5)

    if use_ai:
        model_name = model if model else choose_model()
        chapters    = load_chapters(video_dir)
        skip_ranges = ask_ollama_for_skip_ranges(model_name, srt_entries, chapters, duration)
        clips       = build_clip_plan(
            srt_entries, skip_ranges, duration,
            max_clip_sec=effective_max, min_clip_sec=effective_min,
        )
    else:
        clips = build_fixed_clip_plan(duration, clip_length=effective_max)

    if not clips:
        log_err("No clips could be planned. Exiting.")
        sys.exit(1)

    plan_path = os.path.join(video_dir, "clips_plan.json")
    with open(plan_path, "w") as fh:
        json.dump(clips, fh, indent=2)
    log_ok(f"Plan saved → {plan_path}")

    print(f"\n[Step 2] Done — {len(clips)} clips planned (no video written)")

    return {
        "video_path":  video_path,
        "video_dir":   video_dir,
        "srt_entries": srt_entries,
        "clips":       clips,
        "width":       width,
        "height":      height,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 step2_clip.py <video_dir> [--no-ai] [--max-length SECONDS] [--model NAME]")
        sys.exit(1)
    use_ai = "--no-ai" not in sys.argv[2:]
    max_length = None
    if "--max-length" in sys.argv:
        try:
            max_length = float(sys.argv[sys.argv.index("--max-length") + 1])
        except (IndexError, ValueError):
            pass
    model_arg = None
    if "--model" in sys.argv:
        try:
            model_arg = sys.argv[sys.argv.index("--model") + 1]
        except IndexError:
            pass
    result = clip_pipeline(sys.argv[1], use_ai=use_ai, max_clip_length=max_length, model=model_arg)
    print(json.dumps(result["clips"], indent=2))