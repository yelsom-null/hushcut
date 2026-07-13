# Hushcut

Automatically download videos from YouTube channels you choose — **only within the
date windows you set** — and mute the cursing before they hit your library.

Under the hood: `yt-dlp` fetches new videos + subtitles on a schedule, Hushcut scans
the subtitles for profanity (including YouTube's censored `[ __ ]` auto-caption
marker and your own custom words), and `ffmpeg` silences each hit (video stream is
copied, not re-encoded). A small status dashboard runs on port **8788**.

```
┌─────────────┐   schedule   ┌──────────┐   subtitles   ┌────────┐
│ channels +  │ ───────────▶ │  yt-dlp  │ ────────────▶ │ ffmpeg │ ──▶ /data/clean
│ date windows│              │ download │   word scan   │  mute  │
└─────────────┘              └──────────┘               └────────┘
```

## Quick start (Docker)

```bash
cp config/config.example.yaml config/config.yaml
# edit config/config.yaml — add your channels + date windows
docker compose up -d --build
```

Open **http://localhost:8788** for the dashboard. Clean files land in
`data/clean/<channel>/… (clean).mp4`.

## Configuration (`config/config.yaml`)

```yaml
settings:
  check_interval_minutes: 360    # how often to check channels
  keep_original: false           # keep unmuted originals in data/originals
  mute_lead: 0.4                 # seconds of silence before each word
  mute_tail: 0.3                 # seconds of silence after each word
  sub_langs: "en.*"
  on_missing_subs: copy          # copy | skip (videos with no subtitles)
  extra_words:                   # your custom filter list
    - "example word"

channels:
  - name: "Some Channel"
    url: "https://www.youtube.com/@somechannel/videos"
    from: "2026-07"              # month shorthand = July 1–31, 2026 only
  - name: "Another Channel"
    url: "https://www.youtube.com/@another/videos"
    from: "2026-07-01"           # or explicit range
    to: "2026-07-31"
```

Date windows use the video **upload date**. Omit `from`/`to` to take everything.
Config is re-read every cycle — edit it any time without restarting.

## What's in this repo

- `server/main.py` — the scheduler + downloader + muting pipeline + dashboard
- `docker-compose.yml`, `Dockerfile` — container setup (ffmpeg, yt-dlp, deno included)
- `config/config.example.yaml` — starter config
- `tools/hushcut-helper.py` — local helper for the interactive Hushcut review app
  (paste a URL in the app, preview mutes word-by-word, export a muted copy)

## Notes & caveats

- Detection relies on subtitles. YouTube auto-captions work well — word-level
  timestamps in the raw `.vtt` give precise mute timing, and censored words appear
  as `[ __ ]`, which is matched by default.
- Videos with no subtitles at all are copied through unmuted (or skipped —
  see `on_missing_subs`) and marked with a warning on the dashboard.
- Muting silences audio only; captions burned into the picture aren't touched.
- Be polite: long check intervals (6–24 h) keep YouTube from throttling you.
- The Dockerfile installs `deno` for yt-dlp's YouTube extractor (x86_64 build —
  swap the download URL for `aarch64` on ARM/Raspberry Pi).

## Pushing this to GitHub

```bash
git init
git add .
git commit -m "Hushcut: scheduled channel downloads with profanity muting"
git branch -M main
git remote add origin https://github.com/yelsom-null/hushcut.git
git push -u origin main
```
