# Channel Tracker

Tracks recent uploads across a list of YouTube channels, flags videos that are
outperforming that channel's own normal pace, gives you a filterable dashboard
of everything, and pings you when a new pattern shows up. Runs entirely on
free infrastructure (GitHub Actions + GitHub Pages) — no server, no database
to host, no Make.com credits.

## How it works

Every 6 hours (configurable), a GitHub Actions job runs `scripts/track.py`, which:

1. Pulls each channel's recent uploads from the YouTube Data API (cheap — a
   few hundred quota units per run even at 150 channels, against a free daily
   quota of 10,000).
2. Records a views snapshot for every video still inside the 90-day active
   window (old videos drop out automatically so the workload doesn't grow
   forever).
3. Computes each video's velocity (views ÷ days since upload) and compares it
   to that same channel's own median velocity at a similar age — not a raw
   all-time average, which would be biased toward old videos and skewed by
   past viral hits.
4. Flags anything beating its channel's baseline by 1.75× (tunable) as an
   outlier.
5. Writes `docs/data.json`, which the dashboard (`docs/index.html`, served
   free via GitHub Pages) reads to render a filterable grid of every tracked
   video.
6. If any video *newly* crossed the outlier line since the last run, batches
   them into one Claude API call asking it to spot recurring title/hook
   patterns across channels and niches, then sends you a Telegram message
   with the new outliers and that pattern digest.

Nothing fires if nothing new happened — no daily noise, no calendar-based
digest that misses a video that popped off yesterday.

## Setup

### 1. Get a YouTube Data API key (free)

1. Go to console.cloud.google.com, create a project (or use an existing one).
2. APIs & Services → Library → enable "YouTube Data API v3".
3. APIs & Services → Credentials → Create Credentials → API key.
4. Copy the key — you'll add it as a GitHub secret in step 4.

### 2. Get a Claude API key

1. Go to console.anthropic.com → API Keys → Create Key.
2. Copy it.

### 3. Set up Telegram alerts (free, ~2 minutes)

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
   You'll get a bot token — this is `TELEGRAM_BOT_TOKEN`.
2. Message your new bot anything (so it can see your chat), then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find
   `"chat":{"id": ...}` — that number is `TELEGRAM_CHAT_ID`.

### 4. Create the GitHub repo

1. Create a new **private** GitHub repository.
2. Push everything in this folder to it (`git init`, `git add .`,
   `git commit -m "init"`, add the remote, `git push`).

### 5. Add your secrets

In the repo: Settings → Secrets and variables → Actions → New repository
secret. Add each of these:

- `YOUTUBE_API_KEY`
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

(Until these are added, the script still runs — it just skips the Claude
digest and Telegram send and logs what it would have done, so you can wire
things up gradually.)

### 6. Turn on GitHub Pages

Settings → Pages → Source → "Deploy from a branch" → Branch: `main`, folder:
`/docs` → Save. Your dashboard will be live at
`https://<your-username>.github.io/<repo-name>/` within a minute or two.

### 7. Add your channels

Edit `channels.yaml` and replace the placeholder entries with your real
channels — each needs the channel's YouTube **channel ID** (starts with
`UC...`, not the `@handle`). Easiest way to find it: open the channel →
"..." or About tab → "Share channel" → "Copy channel ID". Commit and push.
Add as many as you like — the same file scales from 3 channels to 150.

### 8. Run it

Actions tab → "Track YouTube Channels" → "Run workflow" to trigger it
immediately instead of waiting for the schedule. Check the run log — if
`channels.yaml` still has placeholder IDs it'll just say so and exit
cleanly. Once it runs successfully with real channels, `docs/data.json` gets
committed and your dashboard fills in.

## Tuning

All the knobs are at the top of `scripts/track.py`:

- `ACTIVE_WINDOW_DAYS` — how long a video stays in the tracked pool (90 by default).
- `OUTLIER_THRESHOLD` — how far above baseline counts as outperforming (1.75× by default).
- `MIN_BASELINE_SAMPLES` — minimum comparable videos needed before trusting a baseline (avoids false positives on brand-new channels).
- The cron schedule in `.github/workflows/track.yml` — every 6 hours by default; go hourly if you want faster detection, quota easily supports it even at 150 channels.

## Notes

- `data/tracker.db` is a SQLite file committed to the repo — it's your full
  history, and it's what lets the outlier math compare against the past.
  Keep the repo private since it'll contain your tracked-channel data.
- The dashboard is a plain static site with no build step — open
  `docs/index.html` in a browser locally too, as long as `docs/data.json`
  sits next to it.
