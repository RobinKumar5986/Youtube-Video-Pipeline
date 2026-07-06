#!/usr/bin/env python3
"""
SortCliper GUI - Basic control panel for the pipeline in main.py

Drop this file into your Cliper project folder (next to main.py) and run:
    python3 gui.py

It shells out to `main.py` for every action, so it stays in sync with
whatever main.py does — no duplicated pipeline logic here.

Requires: pip install customtkinter
"""

import os
import sys
import json
import re
import subprocess
import threading
import queue
import tkinter as tk

import customtkinter as ctk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE_DIR, "RawVideos")
FINAL_DIR = os.path.join(BASE_DIR, "FinalVideos")
MAIN_PY = os.path.join(BASE_DIR, "main.py")
CONFIG_PATH = os.path.join(BASE_DIR, "gui_config.json")

# Mirrors step2_clip.MODELS — kept as plain strings here so the GUI doesn't
# need to import the pipeline modules just to build a dropdown.
MODEL_OPTIONS = {
    "(default)": None,
    "Llama3 8B (slower, smarter)": "llama3:latest",
    "Llama3.2 1B (faster, lighter)": "llama3.2:1b",
}

# Mirrors step4_upload.TARGET_COUNTRIES keys — keep this list in sync if you
# add/remove countries there.
COUNTRY_OPTIONS = [
    "(none)",
    "United States",
    "United Kingdom",
    "Canada",
    "Australia",
    "Germany",
    "Netherlands",
    "Sweden",
    "Ireland",
]

MIN_CLIP_LEN = 15
MAX_CLIP_LEN = 180
DEFAULT_CLIP_LEN = 60

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


# ── Screen-size aware scaling ────────────────────────────────────────────
# We size and scale the whole UI relative to the actual display instead of
# using fixed pixel values, so it looks right on a 13" laptop panel and on
# a 27" monitor alike.

def get_screen_metrics():
    probe = tk.Tk()
    probe.withdraw()
    sw = probe.winfo_screenwidth()
    sh = probe.winfo_screenheight()
    probe.destroy()
    return sw, sh


SCREEN_W, SCREEN_H = get_screen_metrics()

# 1920x1080 is our "1.0x" reference point. Clamp so tiny/huge screens don't
# produce an unusable UI.
_raw_scale = min(SCREEN_W / 1920, SCREEN_H / 1080)
UI_SCALE = max(0.85, min(_raw_scale, 2.0))

# customtkinter scales both widget dimensions and font sizes from this call.
ctk.set_widget_scaling(UI_SCALE)
ctk.set_window_scaling(UI_SCALE)

# Kept at the original, screen-safe size — the log panel gets wider by
# shrinking the control column's share (see grid_columnconfigure below),
# not by growing the window itself.
WIN_W = min(int(SCREEN_W * 0.80), 1760)
WIN_H = min(int(SCREEN_H * 0.78), 1000)
WIN_MIN_W = int(SCREEN_W * 0.55)
WIN_MIN_H = int(SCREEN_H * 0.5)

# Base font sizes (pt) — these get multiplied by UI_SCALE when we build the
# CTkFont objects, on top of customtkinter's own widget scaling, so text
# stays comfortably legible on high-DPI/4K laptop screens too.
FONT_BODY = round(14 * UI_SCALE)
FONT_BOLD = round(15 * UI_SCALE)
FONT_MONO = round(13 * UI_SCALE)
FONT_LABEL_SMALL = round(12 * UI_SCALE)


# ── Config persistence (remembers your last URL / privacy / etc.) ──────────

def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            return json.load(open(CONFIG_PATH))
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    try:
        json.dump(cfg, open(CONFIG_PATH, "w"), indent=2)
    except Exception:
        pass


def list_raw_folders():
    if not os.path.isdir(RAW_DIR):
        return []
    return sorted(f for f in os.listdir(RAW_DIR) if os.path.isdir(os.path.join(RAW_DIR, f)))


# Matches client_secret.json (-> "Default"), client_secret_gaming.json
# (-> "Gaming"), and also bare-numbered/named files like client_secret1.json
# (-> "1") or client_secret-vlogs.json (-> "Vlogs") — mirrors the regex in
# step4_upload.discover_accounts() so both stay in sync.
_CLIENT_SECRET_RE = re.compile(r"^client_secret[_-]?(?P<suffix>[^.]+)?\.json$")


def list_accounts():
    """
    Mirrors step4_upload.discover_accounts()'s naming convention so the GUI
    doesn't need to import that module:
        client_secret.json           -> "Default"
        client_secret_<name>.json    -> "<Name>"
        client_secret<name>.json     -> "<Name>"
    Returns a list of label strings, sorted, "Default" first if present.
    """
    labels = []
    for f in sorted(os.listdir(BASE_DIR)):
        m = _CLIENT_SECRET_RE.match(f)
        if not m:
            continue
        suffix = m.group("suffix")
        labels.append(suffix.replace("_", " ").replace("-", " ").strip().title() if suffix else "Default")
    labels.sort(key=lambda l: (l != "Default", l))
    return labels


# Matches clip1_final.mp4, clip12_final.mp4, etc. — mirrors the filename
# pattern step3_overlay.py renders each clip to.
_FINAL_CLIP_RE = re.compile(r"^clip(?P<num>\d+)_final\.mp4$", re.IGNORECASE)


def list_final_clips(folder_name: str):
    """
    Returns a sorted list of clip index numbers already rendered for the
    given RawVideos folder name, by looking in FinalVideos/<folder_name>/.

    Prefers final_metadata.json (written by step3_overlay.py) since it's the
    authoritative clip -> file mapping; falls back to scanning for
    clip<N>_final.mp4 files if that's missing.
    Returns [] if the folder has no rendered clips yet (or none selected).
    """
    if not folder_name or folder_name == "(none)":
        return []

    final_folder = os.path.join(FINAL_DIR, folder_name)

    meta_path = os.path.join(final_folder, "final_metadata.json")
    if os.path.exists(meta_path):
        try:
            entries = json.load(open(meta_path))
            return sorted(e["clip"] for e in entries)
        except Exception:
            pass

    if not os.path.isdir(final_folder):
        return []

    indices = []
    for f in os.listdir(final_folder):
        m = _FINAL_CLIP_RE.match(f)
        if m:
            indices.append(int(m.group("num")))
    return sorted(indices)


class SortCliperGUI(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("SortCliper Control Panel")
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.minsize(WIN_MIN_W, WIN_MIN_H)

        self.cfg = load_config()
        self.proc = None
        self.log_queue = queue.Queue()
        self.clip_vars = {}  # clip index -> BooleanVar, for the current folder

        self._build_layout()
        self.refresh_folders()
        self.refresh_accounts()
        self.refresh_clips()
        self.after(100, self._drain_log_queue)

    # ── Layout ───────────────────────────────────────────────────────────

    def _build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        body_font = ctk.CTkFont(size=FONT_BODY)
        self.body_font = body_font

        # Left column holds every control section, stacked top to bottom.
        # The log panel lives in its own column on the right (sticky="nsew"
        # below) so it's always visible while you tweak settings or run
        # steps — no need to scroll down to see what's happening.
        left = ctk.CTkFrame(self, fg_color="transparent")
        left.grid(row=0, column=0, sticky="new")
        left.grid_columnconfigure(0, weight=1)

        # -- Section: Full pipeline --
        top = ctk.CTkFrame(left)
        top.grid(row=0, column=0, sticky="ew", padx=(16, 8), pady=(16, 8))
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="YouTube URL", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=0, column=0, padx=(12, 8), pady=(12, 4), sticky="w"
        )

        self.url_entry = ctk.CTkEntry(
            top, placeholder_text="https://youtube.com/watch?v=...",
            font=body_font, height=round(32 * UI_SCALE)
        )
        self.url_entry.grid(row=0, column=1, columnspan=3, padx=(0, 12), pady=(12, 4), sticky="ew")
        self.url_entry.insert(0, self.cfg.get("last_url", ""))

        ctk.CTkLabel(top, text="Privacy", font=body_font).grid(row=1, column=0, padx=(12, 8), pady=4, sticky="w")
        self.privacy_var = ctk.StringVar(value=self.cfg.get("privacy", "private"))
        self.privacy_menu = ctk.CTkOptionMenu(
            top, values=["private", "unlisted", "public"], variable=self.privacy_var,
            font=body_font, height=round(30 * UI_SCALE)
        )
        self.privacy_menu.grid(row=1, column=1, padx=(0, 12), pady=4, sticky="w")

        ctk.CTkLabel(top, text="Channel", font=body_font).grid(row=1, column=2, padx=(0, 8), pady=4, sticky="e")
        self.account_var = ctk.StringVar(value="(default)")
        self.account_menu = ctk.CTkOptionMenu(
            top, values=["(default)"], variable=self.account_var,
            font=body_font, height=round(30 * UI_SCALE)
        )
        self.account_menu.grid(row=1, column=3, padx=(0, 12), pady=4, sticky="w")

        self.no_upload_var = ctk.BooleanVar(value=self.cfg.get("no_upload", False))
        self.no_upload_check = ctk.CTkCheckBox(
            top, text="Skip upload (stop after render)", variable=self.no_upload_var, font=body_font
        )
        self.no_upload_check.grid(row=2, column=0, columnspan=2, padx=12, pady=(4, 12), sticky="w")

        self.run_full_btn = ctk.CTkButton(
            top, text="▶ Run Full Pipeline", command=self.run_full_pipeline,
            height=round(36 * UI_SCALE), font=ctk.CTkFont(size=FONT_BODY, weight="bold")
        )
        self.run_full_btn.grid(row=2, column=2, columnspan=2, padx=(0, 12), pady=(4, 12), sticky="e")

        # -- Section: Clipping + scheduling options --
        opts = ctk.CTkFrame(left)
        opts.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=8)
        opts.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(opts, text="Clipping", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=0, column=0, padx=(12, 8), pady=(12, 6), sticky="w"
        )
        self.ai_clip_var = ctk.BooleanVar(value=self.cfg.get("ai_clip", True))
        self.ai_clip_check = ctk.CTkCheckBox(
            opts, text="Clip by AI (uncheck = fixed clips at the length below, no AI analysis)",
            variable=self.ai_clip_var, font=body_font
        )
        self.ai_clip_check.grid(row=0, column=1, columnspan=3, padx=(0, 12), pady=(12, 6), sticky="w")

        ctk.CTkLabel(opts, text="Publishing", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=1, column=0, padx=(12, 8), pady=(6, 12), sticky="w"
        )
        self.schedule_var = ctk.BooleanVar(value=self.cfg.get("schedule", False))
        self.schedule_check = ctk.CTkCheckBox(
            opts, text="Schedule uploads (drip-publish instead of all at once)",
            variable=self.schedule_var, font=body_font, command=self._on_schedule_toggle
        )
        self.schedule_check.grid(row=1, column=1, padx=(0, 12), pady=(6, 12), sticky="w")

        ctk.CTkLabel(opts, text="Every", font=body_font).grid(row=1, column=2, padx=(12, 6), pady=(6, 12), sticky="e")
        self.schedule_interval_var = ctk.StringVar(value=str(self.cfg.get("schedule_interval", 2)))
        self.schedule_interval_entry = ctk.CTkEntry(
            opts, textvariable=self.schedule_interval_var, width=round(60 * UI_SCALE),
            font=body_font, height=round(30 * UI_SCALE), justify="center"
        )
        self.schedule_interval_entry.grid(row=1, column=3, padx=(0, 4), pady=(6, 12), sticky="w")
        ctk.CTkLabel(opts, text="hours apart", font=body_font).grid(
            row=1, column=4, padx=(4, 12), pady=(6, 12), sticky="w"
        )
        self._on_schedule_toggle()

        # -- Section: Targeting (max length, model, country) --
        target = ctk.CTkFrame(left)
        target.grid(row=2, column=0, sticky="ew", padx=(16, 8), pady=8)
        target.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(target, text="Max short length", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=0, column=0, padx=(12, 8), pady=(12, 6), sticky="w"
        )
        slider_row = ctk.CTkFrame(target, fg_color="transparent")
        slider_row.grid(row=0, column=1, columnspan=3, padx=(0, 12), pady=(12, 6), sticky="ew")
        slider_row.grid_columnconfigure(0, weight=1)

        initial_len = int(self.cfg.get("max_clip_length", DEFAULT_CLIP_LEN))
        initial_len = max(MIN_CLIP_LEN, min(initial_len, MAX_CLIP_LEN))
        self.max_length_var = ctk.IntVar(value=initial_len)
        self.max_length_slider = ctk.CTkSlider(
            slider_row, from_=MIN_CLIP_LEN, to=MAX_CLIP_LEN,
            number_of_steps=MAX_CLIP_LEN - MIN_CLIP_LEN,
            variable=self.max_length_var, command=self._on_length_slide,
        )
        self.max_length_slider.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self.max_length_label = ctk.CTkLabel(
            slider_row, text=f"{initial_len}s", font=body_font, width=round(50 * UI_SCALE)
        )
        self.max_length_label.grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(target, text="Model", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=1, column=0, padx=(12, 8), pady=(6, 6), sticky="w"
        )
        self.model_var = ctk.StringVar(value=self.cfg.get("model_label", "(default)"))
        self.model_menu = ctk.CTkOptionMenu(
            target, values=list(MODEL_OPTIONS.keys()), variable=self.model_var,
            font=body_font, height=round(30 * UI_SCALE)
        )
        self.model_menu.grid(row=1, column=1, padx=(0, 12), pady=(6, 6), sticky="w")

        ctk.CTkLabel(target, text="Target country", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=1, column=2, padx=(12, 8), pady=(6, 6), sticky="e"
        )
        self.country_var = ctk.StringVar(value=self.cfg.get("country", "(none)"))
        self.country_menu = ctk.CTkOptionMenu(
            target, values=COUNTRY_OPTIONS, variable=self.country_var,
            font=body_font, height=round(30 * UI_SCALE)
        )
        self.country_menu.grid(row=1, column=3, padx=(0, 12), pady=(6, 6), sticky="w")

        ctk.CTkLabel(
            target,
            text="Model applies to both clip analysis and metadata generation. Country tailors "
                 "titles/hashtags/scheduling for that audience (source videos stay English).",
            font=ctk.CTkFont(size=FONT_LABEL_SMALL), text_color="#8a8a8a", justify="left", wraplength=round(620 * UI_SCALE),
        ).grid(row=2, column=0, columnspan=4, padx=12, pady=(0, 12), sticky="w")

        # -- Section: Step-by-step + folder select --
        mid = ctk.CTkFrame(left)
        mid.grid(row=3, column=0, sticky="ew", padx=(16, 8), pady=8)
        mid.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(mid, text="Target folder (RawVideos/)", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=0, column=0, padx=(12, 8), pady=(12, 4), sticky="w"
        )
        self.folder_var = ctk.StringVar(value="(none)")
        self.folder_menu = ctk.CTkOptionMenu(
            mid, values=["(none)"], variable=self.folder_var,
            font=body_font, height=round(30 * UI_SCALE), command=self._on_folder_change
        )
        self.folder_menu.grid(row=0, column=1, padx=(0, 8), pady=(12, 4), sticky="w")

        self.refresh_btn = ctk.CTkButton(
            mid, text="↻ Refresh", width=round(86 * UI_SCALE), height=round(30 * UI_SCALE),
            font=body_font, command=self.refresh_all
        )
        self.refresh_btn.grid(row=0, column=2, padx=(0, 12), pady=(12, 4), sticky="w")

        ctk.CTkLabel(mid, text="Steps:", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).grid(
            row=1, column=0, padx=(12, 8), pady=(4, 12), sticky="w"
        )
        step_bar = ctk.CTkFrame(mid, fg_color="transparent")
        step_bar.grid(row=1, column=1, columnspan=4, padx=(0, 12), pady=(4, 12), sticky="ew")

        btn_w = round(128 * UI_SCALE)
        btn_h = round(34 * UI_SCALE)
        self.step1_btn = ctk.CTkButton(
            step_bar, text="1. Download", width=btn_w, height=btn_h, font=body_font, command=self.run_step1
        )
        self.step1_btn.pack(side="left", padx=(0, 6))
        self.step2_btn = ctk.CTkButton(
            step_bar, text="2. Plan clips", width=btn_w, height=btn_h, font=body_font, command=self.run_step2
        )
        self.step2_btn.pack(side="left", padx=6)
        self.step3_btn = ctk.CTkButton(
            step_bar, text="3. Render", width=btn_w, height=btn_h, font=body_font, command=self.run_step3
        )
        self.step3_btn.pack(side="left", padx=6)
        self.step4_btn = ctk.CTkButton(
            step_bar, text="4. Upload", width=btn_w, height=btn_h, font=body_font, command=self.run_step4
        )
        self.step4_btn.pack(side="left", padx=6)

        self.cancel_btn = ctk.CTkButton(
            step_bar, text="■ Cancel", width=round(94 * UI_SCALE), height=btn_h,
            font=body_font, fg_color="#8B2E2E", hover_color="#6E2424",
            command=self.cancel_process
        )
        self.cancel_btn.pack(side="right")

        # -- Section: Clip selection (which rendered clips to upload) --
        clips_frame = ctk.CTkFrame(left)
        clips_frame.grid(row=4, column=0, sticky="ew", padx=(16, 8), pady=8)
        clips_frame.grid_columnconfigure(0, weight=1)

        clips_header = ctk.CTkFrame(clips_frame, fg_color="transparent")
        clips_header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))

        ctk.CTkLabel(
            clips_header, text="Clips to upload (FinalVideos/)",
            font=ctk.CTkFont(size=FONT_BOLD, weight="bold")
        ).pack(side="left")
        self.clips_count_label = ctk.CTkLabel(
            clips_header, text="", font=ctk.CTkFont(size=FONT_LABEL_SMALL), text_color="#8a8a8a"
        )
        self.clips_count_label.pack(side="left", padx=(10, 0))

        clip_btns = ctk.CTkFrame(clips_header, fg_color="transparent")
        clip_btns.pack(side="right")
        ctk.CTkButton(
            clip_btns, text="Select all", width=round(78 * UI_SCALE), height=round(26 * UI_SCALE),
            font=body_font, command=self.select_all_clips
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            clip_btns, text="Deselect all", width=round(88 * UI_SCALE), height=round(26 * UI_SCALE),
            font=body_font, command=self.deselect_all_clips
        ).pack(side="left")

        self.clips_scroll = ctk.CTkScrollableFrame(
            clips_frame, height=round(110 * UI_SCALE), fg_color="transparent"
        )
        self.clips_scroll.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 12))

        ctk.CTkLabel(
            clips_frame,
            text="Unchecked clips are skipped when you hit '4. Upload' — handy for staying under "
                 "YouTube's daily upload limit. All clips are selected by default.",
            font=ctk.CTkFont(size=FONT_LABEL_SMALL), text_color="#8a8a8a", justify="left",
            wraplength=round(620 * UI_SCALE),
        ).grid(row=2, column=0, padx=12, pady=(0, 12), sticky="w")

        # -- Section: Log console (right-hand column, always visible) --
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=16)
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(log_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 0))
        ctk.CTkLabel(header, text="Log", font=ctk.CTkFont(size=FONT_BOLD, weight="bold")).pack(side="left")
        self.status_label = ctk.CTkLabel(
            header, text="Idle", text_color="#8a8a8a", font=ctk.CTkFont(size=FONT_LABEL_SMALL)
        )
        self.status_label.pack(side="right")

        self.log_box = ctk.CTkTextbox(log_frame, wrap="word", font=ctk.CTkFont(family="monospace", size=FONT_MONO))
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=12)
        self.log_box.configure(state="disabled")

    def _on_schedule_toggle(self):
        state = "normal" if self.schedule_var.get() else "disabled"
        self.schedule_interval_entry.configure(state=state)

    def _on_length_slide(self, value):
        self.max_length_label.configure(text=f"{int(round(value))}s")

    def _on_folder_change(self, _choice=None):
        self.refresh_clips()

    # ── Folder handling ─────────────────────────────────────────────────

    def refresh_all(self):
        self.refresh_folders()
        self.refresh_accounts()
        self.refresh_clips()

    def refresh_folders(self):
        folders = list_raw_folders()
        values = folders if folders else ["(none)"]
        self.folder_menu.configure(values=values)
        last = self.cfg.get("last_folder")
        if last in folders:
            self.folder_var.set(last)
        else:
            self.folder_var.set(values[0])

    def selected_folder_index(self, folders):
        """1-based index matching main.py's select_video_dir() ordering."""
        current = self.folder_var.get()
        if current in folders:
            return folders.index(current) + 1
        return 1

    # ── Clip selection handling (which rendered clips get uploaded) ─────

    def refresh_clips(self):
        """Rebuilds the clip checkboxes for whatever folder is currently
        selected, based on what's actually been rendered to FinalVideos/."""
        for w in self.clips_scroll.winfo_children():
            w.destroy()
        self.clip_vars = {}

        folder = self.folder_var.get()
        indices = list_final_clips(folder)

        if not indices:
            ctk.CTkLabel(
                self.clips_scroll,
                text="(no rendered clips found for this folder yet — run step 3)",
                font=ctk.CTkFont(size=FONT_LABEL_SMALL), text_color="#8a8a8a",
            ).grid(row=0, column=0, padx=4, pady=4, sticky="w")
            self.clips_count_label.configure(text="")
            return

        # Restore a previous selection for this folder if we have one,
        # otherwise default to everything checked.
        prev_selected = self.cfg.get("selected_clips", {}).get(folder)

        num_cols = 6
        for i, idx in enumerate(indices):
            r, c = divmod(i, num_cols)
            checked = True if prev_selected is None else (idx in prev_selected)
            var = ctk.BooleanVar(value=checked)
            self.clip_vars[idx] = var
            ctk.CTkCheckBox(
                self.clips_scroll, text=f"Clip {idx}", variable=var, font=self.body_font
            ).grid(row=r, column=c, padx=8, pady=4, sticky="w")

        self.clips_count_label.configure(text=f"{len(indices)} clip(s) found")

    def select_all_clips(self):
        for v in self.clip_vars.values():
            v.set(True)

    def deselect_all_clips(self):
        for v in self.clip_vars.values():
            v.set(False)

    def only_clip_args(self):
        """
        Returns ['--only-clips', '1,3,5'] if the user unchecked at least one
        clip for the currently selected folder, else [] (meaning "upload
        everything", same as main.py's default behavior when the flag is
        omitted entirely).
        """
        folder = self.folder_var.get()
        total = list_final_clips(folder)
        if not total or not self.clip_vars:
            return []

        selected = sorted(idx for idx, v in self.clip_vars.items() if v.get())
        if not selected:
            self._log("⚠ No clips selected — uploading all clips instead.\n")
            return []
        if set(selected) == set(total):
            return []
        return ["--only-clips", ",".join(str(i) for i in selected)]

    def _persist_selected_clips(self, folder):
        if not folder or folder == "(none)" or not self.clip_vars:
            return
        selected = sorted(idx for idx, v in self.clip_vars.items() if v.get())
        all_selected = self.cfg.get("selected_clips", {})
        all_selected[folder] = selected
        self.cfg["selected_clips"] = all_selected

    # ── Channel/account handling ─────────────────────────────────────────

    def refresh_accounts(self):
        labels = list_accounts()
        values = labels if labels else ["(default)"]
        self.account_menu.configure(values=values)
        last = self.cfg.get("last_account")
        if last in labels:
            self.account_var.set(last)
        else:
            self.account_var.set(values[0])

    def selected_account_args(self):
        """Returns ['--account', label] to append to a main.py call, or []
        if there's nothing to pass (no client_secret*.json set up yet)."""
        if not list_accounts():
            return []
        label = self.account_var.get()
        if not label or label == "(default)":
            return []
        return ["--account", label]

    # ── Clipping / scheduling / targeting option handling ────────────────

    def clip_args(self):
        """Returns [] if AI clipping is on (default main.py behavior), or
        ['--no-ai-clip'] if the checkbox is unchecked."""
        return [] if self.ai_clip_var.get() else ["--no-ai-clip"]

    def max_length_args(self):
        """Always passes --max-length so the slider is authoritative."""
        return ["--max-length", str(int(round(self.max_length_var.get())))]

    def model_args(self):
        """Returns ['--model', NAME] unless '(default)' is selected."""
        model_name = MODEL_OPTIONS.get(self.model_var.get())
        return ["--model", model_name] if model_name else []

    def country_args(self):
        """Returns ['--country', NAME] unless '(none)' is selected."""
        country = self.country_var.get()
        return ["--country", country] if country and country != "(none)" else []

    def schedule_args(self):
        """Returns ['--schedule', '--schedule-interval', 'N'] if scheduling
        is enabled, else []. Falls back to 2 (and warns) on a bad interval."""
        if not self.schedule_var.get():
            return []
        raw = self.schedule_interval_var.get().strip()
        try:
            interval = float(raw)
            if interval <= 0:
                raise ValueError
        except ValueError:
            self._log(f"⚠ Invalid schedule interval '{raw}' — using default of 2 hours.\n")
            interval = 2
            self.schedule_interval_var.set("2")
        return ["--schedule", "--schedule-interval", str(interval)]

    # ── Command runners ──────────────────────────────────────────────────

    def run_full_pipeline(self):
        url = self.url_entry.get().strip()
        if not url:
            self._log("❌ Enter a YouTube URL first.\n")
            return
        privacy = self.privacy_var.get()
        args = [url, "--privacy", privacy]
        args += self.clip_args()
        args += self.max_length_args()
        args += self.model_args()
        if not self.no_upload_var.get():
            args += self.selected_account_args()
            args += self.schedule_args()
            args += self.country_args()
        if self.no_upload_var.get():
            args.append("--no-upload")
        self._persist_config(
            url=url, privacy=privacy, no_upload=self.no_upload_var.get(),
            last_account=self.account_var.get(),
            ai_clip=self.ai_clip_var.get(),
            schedule=self.schedule_var.get(),
            schedule_interval=self.schedule_interval_var.get(),
            max_clip_length=int(round(self.max_length_var.get())),
            model_label=self.model_var.get(),
            country=self.country_var.get(),
        )
        self._run(args, stdin_lines=None)

    def run_step1(self):
        url = self.url_entry.get().strip()
        if not url:
            self._log("❌ Step 1 needs a YouTube URL.\n")
            return
        self._persist_config(url=url)
        self._run(["1", url])

    def run_step2(self):
        self._persist_config(
            ai_clip=self.ai_clip_var.get(),
            max_clip_length=int(round(self.max_length_var.get())),
            model_label=self.model_var.get(),
        )
        args = ["2"] + self.clip_args() + self.max_length_args() + self.model_args()
        self._run_with_folder_choice(args)

    def run_step3(self):
        self._run_with_folder_choice(["3"])

    def run_step4(self):
        privacy = self.privacy_var.get()
        self._persist_selected_clips(self.folder_var.get())
        self._persist_config(
            privacy=privacy, last_account=self.account_var.get(),
            schedule=self.schedule_var.get(), schedule_interval=self.schedule_interval_var.get(),
            model_label=self.model_var.get(), country=self.country_var.get(),
        )
        args = ["4", "--privacy", privacy]
        args += self.selected_account_args() + self.schedule_args()
        args += self.model_args() + self.country_args()
        args += self.only_clip_args()
        self._run_with_folder_choice(args)

    def _run_with_folder_choice(self, args):
        folders = list_raw_folders()
        if not folders:
            self._log("❌ No folders in RawVideos/ yet. Run step 1 first.\n")
            return
        idx = self.selected_folder_index(folders)
        self._persist_config(last_folder=folders[idx - 1])
        # main.py only prompts when there's more than one folder; feeding the
        # index on stdin is harmless even if it isn't needed.
        self._run(args, stdin_lines=[str(idx)])

    def cancel_process(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self._log("\n■ Cancelled by user.\n")
            self._set_status("Cancelled", "#c99a2e")

    # ── Subprocess execution ─────────────────────────────────────────────

    def _run(self, args, stdin_lines=None):
        if self.proc and self.proc.poll() is None:
            self._log("⚠ A step is already running. Cancel it first.\n")
            return
        if not os.path.exists(MAIN_PY):
            self._log(f"❌ Couldn't find main.py at {MAIN_PY}\n")
            return

        self._set_buttons_enabled(False)
        self._set_status("Running…", "#3ea6ff")
        self._log(f"\n$ python3 main.py {' '.join(args)}\n")

        def worker():
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, MAIN_PY, *args],
                    cwd=BASE_DIR,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                if stdin_lines:
                    for line in stdin_lines:
                        self.proc.stdin.write(line + "\n")
                    self.proc.stdin.flush()
                self.proc.stdin.close()

                for line in self.proc.stdout:
                    self.log_queue.put(line)

                code = self.proc.wait()
                if code == 0:
                    self.log_queue.put("\n✅ Done.\n")
                    self.log_queue.put(("__status__", "Done", "#3ec96e"))
                else:
                    self.log_queue.put(f"\n❌ Exited with code {code}.\n")
                    self.log_queue.put(("__status__", "Failed", "#e05a5a"))
            except Exception as e:
                self.log_queue.put(f"\n❌ Error launching process: {e}\n")
                self.log_queue.put(("__status__", "Error", "#e05a5a"))
            finally:
                self.log_queue.put(("__enable__",))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple):
                    if item[0] == "__status__":
                        self._set_status(item[1], item[2])
                    elif item[0] == "__enable__":
                        self._set_buttons_enabled(True)
                        self.refresh_all()
                else:
                    self._log(item)
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    # ── UI helpers ───────────────────────────────────────────────────────

    def _log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_status(self, text, color):
        self.status_label.configure(text=text, text_color=color)

    def _set_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        for b in (self.run_full_btn, self.step1_btn, self.step2_btn, self.step3_btn, self.step4_btn):
            b.configure(state=state)

    def _persist_config(self, **kwargs):
        for k, v in kwargs.items():
            self.cfg[k] = v
        self.cfg.setdefault("last_url", self.url_entry.get().strip())
        save_config(self.cfg)


if __name__ == "__main__":
    app = SortCliperGUI()
    app.mainloop()