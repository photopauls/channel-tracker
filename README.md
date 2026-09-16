# Channel Tracker

Tracks recent uploads across a list of YouTube channels, flags videos that are
outperforming that channel's own normal pace, gives you a filterable dashboard
of everything, and pings you when a new pattern shows up. Runs entirely on
free infrastructure (GitHub Actions + GitHub Pages) — no server, no database
to host, no Make.com credits.

## How it works

Every 6 hours (configurable), a GitHub Actions job runs `scripts/track.py`, which:

1. Pulls each channel's uploads from the YouTube Data API. It only pages
   through videos it hasn't seen before (newest first, stopping as soon as it
   hits one already in the database), so this stays cheap even as your back
   catalogue grows into the thousands.
2. Records a fresh views snapshot for **every** tracked video, old and new —
   nothing ages out. A channel's entire history stays tracked and up to date
   for as long as you keep tracking that channel.
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

You don't need to know Python to do any of this — every step below is either
clicking around on a website or copy-pasting one command into a terminal.
Steps 1-2 are only needed if you want to poke at the code on your own
computer before it lives on GitHub; if you just want it running, you can
skip straight to step 3.

### 1. Clone the repository (optional, for testing locally)

You need [Git](https://git-scm.com/downloads) installed first (it comes
built-in on Mac and most Linux; on Windows the installer above is easiest).
Then, in a terminal:

```bash
git clone https://github.com/<your-username-or-this-repo>/channel-tracker.git
cd channel-tracker
```

If you're starting from *this* copy of the project rather than an existing
GitHub repo, just keep this folder as-is and skip the `git clone` — you'll
turn it into your own repo in step 4.

### 2. Install it locally (optional, for testing before you deploy)

This lets you run `scripts/track.py` on your own machine to check it works
before handing it over to GitHub Actions. You need **Python 3.11+**
([python.org/downloads](https://www.python.org/downloads/) if you don't have
it — check with `python3 --version` first).

```bash
# from inside the channel-tracker folder
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r scripts/requirements.txt
```

That installs the two things the script needs (`requests` and `PyYAML`) into
an isolated environment so they don't clash with anything else on your
machine. To actually run it locally you'd also need to set the environment
variables from step 5 below (`export YOUTUBE_API_KEY=...` etc. on Mac/Linux,
`set YOUTUBE_API_KEY=...` on Windows) before `python3 scripts/track.py` — but
most people skip local runs entirely and let step 8's GitHub Actions do the
running instead, since that's free and requires no environment setup at all.

### 3. Get a YouTube Data API key (free)

1. Go to console.cloud.google.com, create a project (or use an existing one).
2. APIs & Services → Library → enable "YouTube Data API v3".
3. APIs & Services → Credentials → Create Credentials → API key.
4. Copy the key — you'll add it as a GitHub secret in step 6.

### 4. Get a Claude API key

1. Go to console.anthropic.com → API Keys → Create Key.
2. Copy it.

### 5. Set up Telegram alerts (free, ~2 minutes)

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts.
   You'll get a bot token — this is `TELEGRAM_BOT_TOKEN`.
2. Message your new bot anything (so it can see your chat), then visit
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and find
   `"chat":{"id": ...}` — that number is `TELEGRAM_CHAT_ID`.

### 6. Create the GitHub repo

1. Create a new **private** GitHub repository (private is important — it'll
   hold your tracked-channel data).
2. Push everything in this folder to it:

   ```bash
   git init
   git add .
   git commit -m "init"
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git branch -M main
   git push -u origin main
   ```

   (Skip `git init` if you already cloned this as a git repo in step 1 —
   just add the remote and push.)

### 7. Add your secrets

In the repo: Settings → Secrets and variables → Actions → New repository
secret. Add each of these:

- `YOUTUBE_API_KEY`
- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

(Until these are added, the script still runs — it just skips the Claude
digest and Telegram send and logs what it would have done, so you can wire
things up gradually.)

### 8. Turn on GitHub Pages

Settings → Pages → Source → "Deploy from a branch" → Branch: `main`, folder:
`/docs` → Save. Your dashboard will be live at
`https://<your-username>.github.io/<repo-name>/` within a minute or two.

### 9. Add your channels

Edit `channels.yaml` and replace the placeholder entries with your real
channels — each needs the channel's YouTube **channel ID** (starts with
`UC...`, not the `@handle`). Easiest way to find it: open the channel →
"..." or About tab → "Share channel" → "Copy channel ID". Commit and push.
Add as many as you like — the same file scales from 3 channels to 150. (Or
skip this and add channels later from the dashboard's "⚙ Manage channels"
panel instead — see below.)

### 10. Run it

Actions tab → "Track YouTube Channels" → "Run workflow" to trigger it
immediately instead of waiting for the schedule. Check the run log — if
`channels.yaml` still has placeholder IDs it'll just say so and exit
cleanly. Once it runs successfully with real channels, `docs/data.json` gets
committed and your dashboard fills in.

## Managing channels from the dashboard

Instead of editing `channels.yaml` by hand every time, the dashboard has a
"⚙ Manage channels" panel that can add and remove channels directly, by
committing straight to `channels.yaml` on GitHub.

To use it: click "⚙ Manage channels", then create a GitHub personal access
token (the panel links straight to the creation page) — a **classic** token
with just the **repo** scope is enough. Paste it in and click "Save token".
The token is stored only in your browser's local storage and used to call
GitHub's API directly from the page; it never goes anywhere else. From there
you can paste a channel URL and tags and click "Add channel", or remove one
from the list — each action commits to `channels.yaml` immediately, and the
change shows up on the dashboard after the next scheduled tracker run (or
trigger one manually from the Actions tab to see it sooner).

A channel can carry more than one tag — type them comma-separated (e.g.
`hairloss, competitors`) in either the "Add channel" row or an existing
channel's tag field, and click Save. Every video from that channel then shows
all of its tags, and the tag-filter pills at the top of the dashboard filter
against any of them, not just one. This replaces the old single "niche" field
— any channel still using `niche: x` in `channels.yaml` keeps working exactly
as before (it's just treated as a one-tag list); editing and re-saving a
channel's tags from this panel moves it over to the new `tags:` list format.

Since the token lives in browser storage rather than a server, only do this
from a device you trust, and use "Forget token" if you ever want to clear it
from that browser.

The list of already-tracked channels stays collapsed behind its own "Show
tracked channels" button inside the panel, so opening "⚙ Manage channels" to
quickly add one doesn't dump 140+ rows on screen every time — click it to
expand the list (and load it fresh from GitHub) when you actually want to
retag or remove something.

## Favourites

There are two kinds: favourite **videos** (the ☆ on each card) and favourite
**channels** (see below). Both work exactly the same way underneath — saved
instantly to this browser's local storage with zero setup, and additionally
committed to a small JSON file in the repo once you've saved a GitHub token,
which is what makes them permanent: they then survive clearing this browser
and show up the same way on any other device you open the dashboard from
(reading either file needs no token; only writing a change does).

Favourite videos are stored in `docs/favorites.json`, updated whenever you
click a video's ☆.

## Favourite channels & the Channels view

Click "Channels" at the top of the dashboard (next to "Videos") to switch to a
page listing every tracked channel, grouped under each of its tags — a channel
with more than one tag shows up under all of them. Click the ☆ next to a
channel there to favourite it; favourite channels are stored in
`docs/favorite_channels.json`, synced the same way as favourite videos.

Back in the Videos view, turning on "Favourite channels only" shows every
video from any channel you've favourited — handy for a quick feed of just the
handful of channels you actually care about, cutting across whatever tags
they're filed under.

## Tuning

All the knobs are at the top of `scripts/track.py`:

- There's no age cutoff by default — every video a channel has ever posted stays tracked and gets its view count refreshed on every run. Set `since: YYYY-MM-DD` on a specific channel in `channels.yaml` if you want to limit just that channel to videos from a certain date forward.
- `OUTLIER_THRESHOLD` — how far above baseline counts as outperforming (1.5× by default). At 1.5×, expect a fairly loose net — in a spot-check against a real, fairly active tracker database this flagged roughly a third of all tracked videos. Raise it (2×, 3×, 5×) if that turns out to be more noise than signal once you're seeing it run for real.
- `MIN_BASELINE_SAMPLES` — minimum comparable videos needed before trusting a baseline (avoids false positives on brand-new channels). Until a channel has this many videos tracked at all, its videos just won't be flagged either way — that's expected, not a bug.
- `TRAILING_COMPARISON_VIDEOS` — a video's baseline is the median **view count** of the N other videos on the channel whose publish date is closest to its own (whichever side is nearer — published just before it, or just after), not a fixed percentage window. Two videos published close together are being judged against genuine same-era peers, so this still tracks the channel's current scale rather than blending in its whole history — it's just resilient to a channel not yet having enough videos in some fixed window, which used to leave videos un-flagged for no good reason.
- The cron schedule in `.github/workflows/track.yml` — every 6 hours by default; go hourly if you want faster detection, quota easily supports it even at 150 channels.

### Fixed bugs worth knowing about

Earlier versions of the outlier check could end up flagging *every* video on a channel as an outlier, and — worse — that could get permanently stuck that way. The root cause: each video's baseline excluded any video already flagged as an outlier, on the theory that one hit shouldn't drag its own comparison average up. Two problems stemmed from that one design choice:

1. It was reading that flag live, mid-run, from videos processed earlier in that *same* run (run order is somewhat arbitrary), so one early false positive could knock itself out of the pool and shrink/skew what later videos in the same run were compared against, snowballing within a single run.
2. Worse, the exclusion was permanent across runs too: once a channel had a bad run where most of its videos got wrongly flagged, every future run's baseline kept getting computed from only the tiny handful of videos that had never been flagged (almost always old, decayed ones with unusually low velocity) — which produced an artificially low baseline that re-flagged nearly everything again, forever. A channel could never recover from this on its own once it happened.

Both are fixed now by simply not excluding flagged outliers from the comparison pool at all. The baseline is a *median*, which is naturally resistant to a minority of real outliers sitting in the pool — it only shifts if more than half the comparison videos are outliers, and at that point that's a genuine change in the channel's scale, not noise. Removing the exclusion makes the whole system self-correcting instead of self-entrenching.

Separately, the baseline used to require a candidate video to fall inside a fixed +/-40% age window before it could be counted at all — which meant a channel that hadn't yet built up enough tracked videos in that exact window (very common right after a backfill) would show "n/a" and never get flagged, no matter how much it was actually outperforming. It's fixed now by instead just taking the N closest-in-age videos, whichever side they fall on, so a baseline is available as soon as the channel has any reasonable amount of comparable history at all.

A third, unrelated bug produced the same symptom (a channel where nearly everything gets flagged) even after the two fixes above: the baseline query never filtered out Shorts. Shorts are supposed to be completely excluded from tracking, but a video can occasionally get one snapshot recorded before it's correctly classified (e.g. a run where YouTube's API briefly didn't return duration data for it) — that lone snapshot then sits in the database forever, since a classified Short never gets processed again to get a fresh one. Because Shorts naturally have tiny view counts, if several of them happened to land near a video's age, they could dominate its "nearest 15" comparison pool and crater the baseline. Fixed by restricting the baseline query to non-Short videos only, matching what the rest of the pipeline already treats as trackable.

A fourth bug showed up specifically on brand-new uploads (a few hours to under a day old) across many different channels at once: nearly every one of them got flagged, often with absurd ratios (10x, 50x, even 180x baseline). The root cause was the metric itself, not a query bug: the outlier check used to compare each video's *velocity* (views ÷ days since upload). Every video gets a disproportionate burst of views in its first few hours (subscriber notifications, the algorithm testing it), so its velocity is highest right when it's brand new and decays hard over the following day or so — that has nothing to do with how good the video actually is. Unless a channel happened to have another video that was *also* only hours old right now (rare for anything but the highest-cadence channels), the nearest comparable videos in the baseline pool were measured at their long-since-settled, decayed velocity — so a fresh video looked like a huge outlier purely because of *when* it was being measured, not because it was exceptional.

The fix was to stop comparing velocity altogether and compare raw view counts instead: a video's current view count against the median current view count of similarly-timed videos on the same channel. There's no rate to decay, so there's no early-hours spike to accidentally compare against everyone else's settled-down state — a video only clears the bar by having genuinely out-accumulated its peers by the time it's checked, which can still happen within hours for something that's really taking off, and confirmed cleanly on real data: 0 of 19 videos under a day old in a live tracker database now get flagged, versus 18 of those same 21 that were wrongly flagged under the velocity-based version. `MIN_AGE_FOR_OUTLIER_DAYS`, the earlier band-aid that just refused to flag anything under a day old, is gone — it's no longer needed with this metric, and it would have delayed catching a video that's genuinely exploding in its first hours.

## Notes

- `data/tracker.db` is a SQLite file committed to the repo — it's your full
  history, and it's what lets the outlier math compare against the past.
  Keep the repo private since it'll contain your tracked-channel data.
- The dashboard is a plain static site with no build step — open
  `docs/index.html` in a browser locally too, as long as `docs/data.json`
  sits next to it.
