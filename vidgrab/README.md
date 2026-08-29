# vidgrab

Paste a link, get the video back — at the best quality that still fits your
upload limit. Telegram is the primary interface; a small web page does the
same job from a browser.

## How the quality ladder works

Telegram caps bot uploads at 50 MB, so "best quality" and "actually sends"
are often different things. vidgrab resolves that by walking down a ladder:

```
1080p → 720p → 480p → 360p → 240p → audio only
```

Each rung is attempted with `max_filesize` set, so yt-dlp aborts before
transferring anything whenever the size is known up front. Streams that
don't declare a size (HLS and friends) are checked after the fact and
discarded if they came out too big. The first rung that fits is what you
get, and the reply tells you which rungs were skipped and why.

## Telegram

| Send | You get |
| --- | --- |
| `<link>` | best video under the upload limit |
| `/audio <link>` | audio only — useful for long recordings |
| `/q 480 <link>` | starts at 480p instead of the top of the ladder |
| `/status` | what's queued or downloading |
| `/help` | the command list |

Multiple links in one message are queued individually. Progress is edited
into the reply as it goes.

## Setup

1. Get a bot token from [@BotFather](https://t.me/BotFather).
2. Get your numeric chat ID (message [@userinfobot](https://t.me/userinfobot)).
3. Set the environment and run it:

```bash
pip install -r vidgrab/requirements.txt        # plus ffmpeg on the PATH
export VIDGRAB_TELEGRAM_TOKEN=123456:ABC...
export VIDGRAB_ALLOWED_CHAT_IDS=8486909237
python -m vidgrab
```

`ffmpeg` is not strictly required, but without it yt-dlp can only use
formats that already carry video and audio in one stream — which caps you
around 360–720p on many sites. The Dockerfile installs it.

### Deploying on Railway

Create a **second service** in the project pointed at this repo, with the
Dockerfile path set to `vidgrab/Dockerfile` (leave the root directory blank
so the build context stays at the repo root). Set the variables below, then
generate a domain if you want the web UI.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `VIDGRAB_TELEGRAM_TOKEN` | falls back to `TELEGRAM_TOKEN` | bot token |
| `VIDGRAB_ALLOWED_CHAT_IDS` | falls back to `TELEGRAM_CHAT_ID` | comma-separated chat IDs allowed to use the bot |
| `VIDGRAB_QUALITY_LADDER` | `1080,720,480,360,240` | the rungs, highest first |
| `VIDGRAB_MAX_UPLOAD_MB` | `48` | size to fit under for Telegram |
| `VIDGRAB_AUDIO_FALLBACK` | `true` | fall back to audio when no rung fits |
| `VIDGRAB_MAX_DURATION_MIN` | `240` | refuse longer media; `0` disables |
| `VIDGRAB_WEB_ENABLED` | `true` | serve the browser UI |
| `PORT` | `8080` | web UI port |
| `VIDGRAB_WEB_TOKEN` | _empty_ | required as `?t=…` when set — set it on any public host |
| `VIDGRAB_MAX_WEB_MB` | `4096` | size ceiling for browser downloads |
| `VIDGRAB_DOWNLOAD_DIR` | system temp | scratch space for downloads |
| `VIDGRAB_FILE_TTL_MIN` | `60` | how long finished files stay fetchable |
| `VIDGRAB_COOKIES_FILE` | _empty_ | Netscape cookie file for sites needing a login |
| `VIDGRAB_COOKIES_FROM_BROWSER` | _empty_ | e.g. `chrome`, when running locally |
| `VIDGRAB_PROXY` | _empty_ | proxy for yt-dlp |

### Sending files larger than 50 MB

Point `TELEGRAM_API_BASE` at a [local Bot API server][local-api] and raise
`VIDGRAB_MAX_UPLOAD_MB` to `2000`. The ladder then rarely has to step down
at all.

[local-api]: https://core.telegram.org/bots/api#using-a-local-bot-api-server

## The web UI

`http://<host>:8080/?t=<VIDGRAB_WEB_TOKEN>` — one box, a quality dropdown
(including "Fit 48MB", the Telegram target), and a progress bar. Finished
files download straight from the server and are deleted after
`VIDGRAB_FILE_TTL_MIN`. **Set `VIDGRAB_WEB_TOKEN` before exposing it**;
without one, anyone who finds the URL can queue downloads on your box.

## Layout

```
vidgrab/
  main.py        entry point — starts worker, web server, bot
  downloader.py  yt-dlp wrapper + the quality ladder
  jobs.py        queue, one worker, TTL cleanup of finished files
  bot.py         Telegram commands and progress reporting
  telegram.py    thin Bot API client
  web.py         paste-a-link page and its JSON endpoints
  tests.py       ladder tests with faked transfers — `python -m vidgrab.tests`
```

## Tests

```bash
python -m vidgrab.tests
```

No network needed — the download attempts are faked so the stepping-down
logic is what gets checked.

## A note on what you download

This is a personal tool built around [yt-dlp](https://github.com/yt-dlp/yt-dlp).
Whether a given link is yours to download is between you and the site's
terms — the tool doesn't know and doesn't decide.
