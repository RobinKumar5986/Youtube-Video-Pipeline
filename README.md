# 🎬 Youtube-Video-Pipeline

A desktop automation tool that takes a long-form YouTube video and turns it into ready-to-publish Shorts — download, AI-powered clip selection, rendering, and scheduled upload, all from one control panel.

<p align="center">
  <img width="900" alt="Youtube-Video-Pipeline GUI - Pipeline & Options" src="https://github.com/user-attachments/assets/a6766d9f-c25a-4924-b339-3510a14130e7" />
</p>

<p align="center">
  <img width="900" alt="Youtube-Video-Pipeline GUI - Clip Selection & Manual Upload" src="https://github.com/user-attachments/assets/fab2f419-e609-4db6-b8cc-5793f7a7778e" />
</p>

---

## ✨ Features

- **Full pipeline in one click** — download → AI clip planning → caption/overlay render → upload
- **Step-by-step control** — run any of the four pipeline stages independently (`1. DL`, `2. Plan`, `3. Render`, `4. Upload`)
- **AI-assisted clipping** with a fixed-length fallback if you'd rather skip the AI analysis
- **Auto-captioning + gameplay overlay** — burns in captions and the clip's title, and stacks a gameplay video in the bottom half of the frame for audience retention
- **Adjustable max clip length** via slider (15s–180s)
- **Scheduled / drip publishing** — space uploads N hours apart starting on a chosen date
- **Per-country targeting** — AI-generated titles, descriptions, and hashtags tailored to the audience you pick, plus country-aware scheduling
- **Multi-channel support** — auto-detects any `client_secret*.json` files as separate uploadable accounts
- **Selective clip upload** — check/uncheck individual rendered clips before hitting upload, useful for staying under YouTube's daily upload cap
- **Manual/standalone upload** — upload any pre-edited video + optional `.srt` transcript and thumbnail directly, without running it through the download/clip/render stages
- **Screen-size aware UI** — scales cleanly from laptop panels to large monitors
- **Live log console** with run status and cancel support

## 🧩 How It Works

The GUI is a control panel only — it doesn't duplicate any pipeline logic. Every action shells out to `main.py`, so the GUI always stays in sync with whatever the pipeline actually does. There are 4 core pipeline stages plus a manual-upload mode:

| Step | Script | What it does | Command |
|------|--------|---------------|---------|
| 1 | `step1_download.py` | Downloads the source YouTube video (via `yt-dlp`) along with related assets like the `.srt` transcript, into `RawVideos/<video>/` | `python main.py 1 <url>` |
| 2 | `step2_clip.py` | Plans the clips — either AI-assisted (Ollama analyzes the transcript to find natural clip boundaries, skip ads/intros/outros, and snap to sentence boundaries) or fixed-length cuts with `--no-ai-clip` | `python main.py 2 [options]` |
| 3 | `step3_overlay.py` | Renders each planned clip: burns in captions and the clip's title/part name, and stacks a gameplay video in the bottom half of the frame (50/50 vertical split) to boost audience retention. Output goes to `FinalVideos/<video>/` | `python main.py 3` |
| 4 | `step4_upload.py` | Generates AI metadata (title, description, hashtags) targeted at the chosen country, then uploads the rendered clips to YouTube via the YouTube Data API v3 — immediately or on a drip schedule | `python main.py 4 [options]` |
| 5 | `step4_upload.py` (`manual_upload_pipeline`) | **Manual upload** — skips steps 1–3 entirely. Takes a video you edited yourself, generates AI metadata the same way Step 4 does, and uploads it directly | `python main.py 5 <video> [srt] [options]` |

Running `main.py` with a YouTube URL directly (no step number) chains steps 1 → 2 → 3 → 4 automatically:

```bash
python main.py <youtube_url> [--privacy private|unlisted|public] [--account NAME] [--no-upload] \
                [--no-ai-clip] [--schedule] [--schedule-interval HOURS] [--schedule-date YYYY-MM-DD] \
                [--max-length SECONDS] [--model NAME] [--country "United States"]
```

<details>
<summary><strong>Full CLI reference (click to expand)</strong></summary>

| Flag | Applies to | Description |
|------|-----------|--------------|
| `--privacy` | full run, 4, 5 | `private` \| `unlisted` \| `public` (default: `private`) |
| `--no-upload` | full run | Skip Step 4 and stop after rendering |
| `--account NAME` | full run, 4, 5 | Which YouTube channel to upload to (matches `client_secret_NAME.json`). Auto-picked if only one exists |
| `--no-ai-clip` | full run, 2 | Disable AI clip planning; cut the video into fixed-length clips instead |
| `--max-length SECONDS` | full run, 2 | Max clip length — upper bound in AI mode, exact length in fixed mode. Default: 150s (AI) / 60s (fixed) |
| `--model NAME` | full run, 2, 4, 5 | Ollama model used for both clip analysis and metadata generation |
| `--country "United States"` | full run, 4, 5 | Target audience for generated titles/descriptions/hashtags and schedule timing (see `step4_upload.TARGET_COUNTRIES`) |
| `--schedule` | full run, 4, 5 | Drip-publish clips instead of uploading them all at once |
| `--schedule-interval HOURS` | full run, 4 | Hours between each scheduled upload (default: 2) |
| `--schedule-date YYYY-MM-DD` | full run, 4, 5 | Anchor date the schedule starts from (default: today) |
| `--only-clips 1,3,5` | 4 | Upload only the listed clip numbers instead of everything rendered |
| `--type short\|long` | 5 | Force video type instead of auto-detecting from aspect ratio |
| `--thumbnail IMAGE_PATH` | 5 | Custom thumbnail to set after upload (requires a phone-verified channel) |

</details>

## 📋 Prerequisites

- Python 3.9+
- [ffmpeg](https://ffmpeg.org/) installed and available on your `PATH`
- [Ollama](https://ollama.com/) running locally (for AI-based clip planning)
- A Google Cloud project with the **YouTube Data API v3** enabled, and an OAuth `client_secret.json` downloaded into the project root

## 🚀 Installation

```bash
# 1. Clone the repository
git clone https://github.com/<your-username>/Youtube-Video-Pipeline.git
cd Youtube-Video-Pipeline

# 2. Create a virtual environment
python3 -m venv venv

# 3. Activate it
source venv/bin/activate      # macOS / Linux
venv\Scripts\activate         # Windows

# 4. Install dependencies
pip install -r requirements.txt
```

## ▶️ Running the App

```bash
python gui.py
```

This launches the SortCliper Control Panel. From there you can run the full pipeline or step through it manually.

> **Tip:** `tkcalendar` powers the date-picker widgets used for scheduling. If it isn't installed, the GUI still runs fine — the calendar just falls back to a plain text field.

## 📁 Project Structure

```
Youtube-Video-Pipeline/
├── gui.py                 # Control panel
├── main.py                # Pipeline entrypoint / CLI (steps 1-5)
├── step1_download.py      # Step 1: download source video + transcript
├── step2_clip.py          # Step 2: AI or fixed-length clip planning
├── step3_overlay.py       # Step 3: captions, title overlay, gameplay split, render
├── step4_upload.py        # Step 4/5: AI metadata + YouTube upload (pipeline & manual)
├── requirements.txt
├── gui_config.json        # Auto-created — remembers your last settings
├── client_secret*.json    # YouTube OAuth credentials (one per channel)
├── RawVideos/             # Downloaded source videos, one folder per video
├── GameplayVideos/        # Contains the oerlat gemeplay videos (random selection)
└── FinalVideos/           # Rendered clips, ready for upload
```

## ⚙️ Multi-Channel Setup

To upload to more than one YouTube channel, add extra OAuth credential files to the project root using this naming pattern — the GUI will pick them up automatically as separate "Channel" options:

```
client_secret.json           → "Default"
client_secret_gaming.json    → "Gaming"
client_secret_vlogs.json     → "Vlogs"
```

## 🖼️ Manual Upload

Already edited a video outside this pipeline? Use the **Manual Upload** section (Step 5) to upload it directly — no need to run it through download/clip/render first:

- Pick a video file; a same-named `.srt` next to it is auto-detected as the transcript if you don't pick one yourself
- Short vs. long-form is auto-detected from the video's pixel aspect ratio (or force it with `--type`)
- AI generates the title, description, and hashtags for it, targeted at the country you choose — same generation logic as Step 4
- Optionally set a custom thumbnail (requires a phone-verified channel) and/or schedule the publish date
- Uses its own independent **Channel** and **Model** selectors, separate from the main pipeline's settings


## 📝 License
 
This project is licensed under the [MIT License](LICENSE).
 
## 💖 Support This Project
 
If this project saved you time, please consider supporting its development — it really helps keep it maintained and growing. You can sponsor or donate via GitHub Sponsors, or just star the repo. Every bit of support is genuinely appreciated! 🙏
