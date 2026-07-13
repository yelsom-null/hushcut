# Hushcut

Automatically download videos from YouTube channels you choose — **only within the
date windows you set** — and mute the cursing before they hit your library.

Under the hood: `yt-dlp` fetches new videos + subtitles on a schedule, Hushcut scans
the subtitles for profanity (including YouTube's censored `[ __ ]` auto-caption
marker and your own custom words), and `ffmpeg` silences each hit (video stream is
copied, not re-encoded). A dashboard on port **8788** shows status and lets you
manage channels and settings right from the browser.

```
┌─────────────┐   schedule   ┌──────────┐   subtitles   ┌────────┐
│ channels +  │ ───────────▶ │  yt-dlp  │ ────────────▶ │ ffmpeg │ ──▶ /data/clean
│ date windows│              │ download │   word scan   │  mute  │
└─────────────┘              └──────────┘               └────────┘
```

## Quick start (Docker)

```bash
docker compose up -d --build
```

Open **http://localhost:8788**, add your channels + date windows in the
**Channels** card, tweak the **Settings** card, hit **Save config**, then
**Check now** (or wait for the schedule). Clean files land in
`data/clean/<channel>/… (clean).mp4`.

A default `config/config.yaml` is created on first run. Prefer editing YAML by
hand? Copy `config/config.example.yaml` to `config/config.yaml` instead — the
dashboard and the file stay in sync (saving from the GUI rewrites the file).

The dashboard's **Activity** card shows what's happening live: which channel is
being checked, per-video download progress (percent + ETA from yt-dlp), muting
progress, and a running log of everything the server does.

## Updating

```bash
./tools/update.sh
```

That pulls the latest commit and applies it: server code is bind-mounted into
the container, so normal updates are just a container restart; the image is
rebuilt automatically when `Dockerfile`/`docker-compose.yml` changed. For
hands-off updates, run it from cron:

```cron
0 4 * * * cd /path/to/hushcut && ./tools/update.sh >> data/update.log 2>&1
```

## Configuration (`config/config.yaml`)

Everything below can be set from the dashboard; the YAML is the source of truth.

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
Config is re-read every cycle — edit it any time (GUI or file) without
restarting; use the dashboard's **Check now** button to apply changes
immediately.

## What's in this repo

- `server/main.py` — the scheduler + downloader + muting pipeline + dashboard
- `docker-compose.yml`, `Dockerfile` — container setup (ffmpeg, yt-dlp, deno included)
- `config/config.example.yaml` — starter config
- `tools/update.sh` — pull the latest commit and restart/rebuild the container
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
- The dashboard has no authentication, and saving config is a write operation —
  keep port 8788 on your home LAN (or bind it to `127.0.0.1:8788:8788` in
  `docker-compose.yml`); don't expose it to the internet.
