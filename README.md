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
progress, and a running log of everything the server does. While a check is
running, the **Check now** button becomes **Stop** — it kills the current
download/mute and ends the check; anything unfinished is retried next cycle.

## YouTube bot checks

If the Activity log shows `Sign in to confirm you're not a bot`, YouTube is
suspicious of your IP. Hushcut ships two defenses; they stack.

### 1. PO token provider (on by default, no account needed)

`docker-compose.yml` runs a companion container, `bgutil-provider`
([bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)),
that generates the "proof of origin" tokens YouTube expects from legitimate
clients, and yt-dlp is pointed at it automatically (the `HUSHCUT_POT_URL` env
var). Nothing to configure — `docker compose up -d --build` starts both. Sync
lines in the Activity log show `[pot]` when it's active. Note the upstream
project's caveat: tokens make traffic look more legitimate but don't guarantee
bypassing every check. To opt out, remove the `bgutil-provider` service and the
`HUSHCUT_POT_URL` line from `docker-compose.yml`.

### 2. Cookies from a signed-in session (fallback)

If the bot check persists, give yt-dlp cookies from a signed-in YouTube
session.

1. In a browser signed in to YouTube (a throwaway Google account is strongly
   recommended — heavy downloading can get an account flagged), install a
   cookies exporter such as **Get cookies.txt LOCALLY**.
2. Export cookies for `youtube.com` in Netscape format. Tip from the yt-dlp
   FAQ: open a private/incognito window, sign in, export from there, then close
   the window — that keeps YouTube from rotating the exported cookies.
3. Save the file as **`config/cookies.txt`** (next to `config.yaml`).

That's it — Hushcut checks for the file on every sync and passes it to yt-dlp
automatically (a green **cookies** pill appears on the dashboard). Requests are
also spaced out (`--sleep-requests 1`) to stay under the radar. More detail:
[yt-dlp FAQ on cookies](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp).

## Updating

```bash
./tools/update.sh
```

That pulls the latest commit and rebuilds the container. Docker's layer cache
makes code-only updates take just a few seconds; it's also safe to run any time
to make sure the container is up. For hands-off updates, run it from cron:

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
- `tools/update.sh` — pull the latest commit and rebuild/restart the container
- `tools/hushcut-helper.py` — local helper for the interactive Hushcut review app
  (paste a URL in the app, preview mutes word-by-word, export a muted copy)

## Troubleshooting

- **Container won't start / keeps restarting:** `docker compose logs --tail 50`
  shows the reason.
- **"container name \"/hushcut\" is already in use"** (happens after re-cloning
  or renaming the repo folder): `docker rm -f hushcut`, then
  `docker compose up -d --build`.
- **Clean rebuild:** `docker compose down && docker compose up -d --build`.

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
