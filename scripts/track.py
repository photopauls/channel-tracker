"""
YouTube channel tracker.

Runs on a schedule (see .github/workflows/track.yml). Each run:
  1. Resolves every channel in channels.yaml (a pasted URL, or a raw channel ID)
     to its real channel ID, caching the lookup so it only happens once per channel.
  2. Pulls recent uploads for each channel (YouTube Data API).
  3. Records a views snapshot for every video still inside the active tracking window.
  4. Computes each video's velocity (views per day since upload) and compares it to
     that channel's own historical baseline at a similar age -> flags outliers.
  5. Writes docs/data.json, the file the static dashboard (docs/index.html) reads.
  6. If any video newly crossed the outlier threshold since the last run, batches
     them into one Claude API call for a pattern digest, then sends a Telegram alert.

All secrets are read from environment variables (set as GitHub Actions secrets).
Nothing here runs API calls at import time, so it's safe to read even before you've
filled in real keys.
"""

import os
import re
import json
import sqlite3
import statistics
import time
import datetime
from urllib.parse import urlparse

import requests
import yaml

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "PLACEHOLDER_YOUTUBE_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "PLACEHOLDER_ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PLACEHOLDER_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PLACEHOLDER_TELEGRAM_CHAT_ID")

# --- tunables -----------------------------------------------------------
# There is no age cutoff by default - every video a channel has ever uploaded
# stays tracked and gets its view count refreshed every run. Set a per-channel
# 'since: YYYY-MM-DD' in channels.yaml if you want to limit a specific channel
# to only its videos from that date forward instead of its full history.
OUTLIER_THRESHOLD = 1.75      # velocity must beat this multiple of the channel baseline
MIN_BASELINE_SAMPLES = 4      # need at least this many comparable videos to trust a baseline
TRAILING_COMPARISON_VIDEOS = 15  # baseline = median velocity of the N other videos on this
                                  # channel whose current age is closest to this video's -
                                  # whichever direction is nearer (published just before it,
                                  # or just after), not a fixed window. A similar-age video was
                                  # necessarily uploaded around the same time, so this still
                                  # tracks the channel's current scale rather than a lifetime
                                  # average, without a rigid tolerance band that can come up
                                  # short of candidates on a channel with thin/backfilled history.
EXCLUDE_SHORTS = True          # Shorts are identified by duration and skipped entirely -
SHORTS_MAX_SECONDS = 180        # not stored, not shown, not counted in the outlier baseline.
                                 # YouTube's own Shorts limit is 3 minutes; drop to 60 if you
                                 # want the older, stricter definition.
CLAUDE_MODEL = "claude-sonnet-4-5"
# -------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB_PATH = os.path.join(ROOT, "data", "tracker.db")
DATA_JSON_PATH = os.path.join(ROOT, "docs", "data.json")
CHANNELS_YAML = os.path.join(ROOT, "channels.yaml")

YT_API = "https://www.googleapis.com/youtube/v3"

TRAILING_SEGMENTS = ("videos", "about", "featured", "playlists", "community", "streams", "shorts")


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            name TEXT,
            niche TEXT,
            uploads_playlist_id TEXT
        );
        CREATE TABLE IF NOT EXISTS videos (
            video_id TEXT PRIMARY KEY,
            channel_id TEXT,
            title TEXT,
            published_at TEXT,
            thumbnail_url TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            video_id TEXT,
            checked_at TEXT,
            view_count INTEGER,
            days_since_upload REAL,
            velocity REAL
        );
        CREATE TABLE IF NOT EXISTS outlier_state (
            video_id TEXT PRIMARY KEY,
            is_outlier INTEGER DEFAULT 0,
            first_flagged_at TEXT
        );
        CREATE TABLE IF NOT EXISTS channel_lookup (
            ref TEXT PRIMARY KEY,
            channel_id TEXT,
            resolved_name TEXT
        );
        """
    )
    conn.commit()
    _ensure_column(conn, "videos", "is_short", "INTEGER DEFAULT 0")
    conn.commit()


def _ensure_column(conn, table, column, coltype):
    """Adds a column to an existing table if it's not already there - lets an
    older tracker.db (from before this column existed) upgrade in place instead
    of needing to be recreated."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration_seconds(iso_duration):
    if not iso_duration:
        return None
    m = DURATION_RE.match(iso_duration)
    if not m:
        return None
    h, mnt, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mnt * 60 + s


def load_channel_entries():
    with open(CHANNELS_YAML) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("channels", []) or []


def yt_get(path, params):
    params = {**params, "key": YOUTUBE_API_KEY}
    r = requests.get(f"{YT_API}/{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def parse_channel_ref(entry):
    """Figure out what kind of reference an entry gives us: a raw channel ID,
    an @handle, or a legacy /c/ or /user/ vanity name."""
    if entry.get("id"):
        return {"type": "id", "value": entry["id"]}

    url = (entry.get("url") or "").strip()
    if not url:
        return None

    path = urlparse(url).path if "://" in url else url
    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[-1].lower() in TRAILING_SEGMENTS:
        parts = parts[:-1]
    if not parts:
        return None

    first = parts[0]
    if first == "channel" and len(parts) > 1:
        return {"type": "id", "value": parts[1]}
    if first.startswith("@"):
        return {"type": "handle", "value": first}
    if first in ("c", "user") and len(parts) > 1:
        return {"type": "legacy", "value": parts[1]}
    # bare handle with no leading @ and no slash, e.g. someone pasted just "BlackMensBeard"
    if len(parts) == 1:
        return {"type": "handle", "value": "@" + first.lstrip("@")}
    return None


def resolve_channel_id(conn, ref):
    """Resolve a parsed reference to (channel_id, display_name), using a cached
    lookup table so each channel only costs API quota once, ever."""
    cache_key = f"{ref['type']}:{ref['value']}"
    row = conn.execute(
        "SELECT channel_id, resolved_name FROM channel_lookup WHERE ref=?", (cache_key,)
    ).fetchone()
    if row and row[0]:
        return row[0], row[1]

    channel_id, name = None, None

    if ref["type"] == "id":
        channel_id = ref["value"]
        data = yt_get("channels", {"part": "snippet", "id": channel_id})
        items = data.get("items", [])
        if items:
            name = items[0]["snippet"]["title"]

    elif ref["type"] == "handle":
        data = yt_get("channels", {"part": "snippet", "forHandle": ref["value"]})
        items = data.get("items", [])
        if items:
            channel_id = items[0]["id"]
            name = items[0]["snippet"]["title"]

    elif ref["type"] == "legacy":
        data = yt_get("channels", {"part": "snippet", "forUsername": ref["value"]})
        items = data.get("items", [])
        if items:
            channel_id = items[0]["id"]
            name = items[0]["snippet"]["title"]
        else:
            # Old /c/ vanity URLs aren't always real "usernames" - fall back to a
            # search lookup. Costs more quota (100 units) so this only fires once
            # per channel, ever, thanks to the cache above.
            data = yt_get("search", {"part": "snippet", "q": ref["value"], "type": "channel", "maxResults": 1})
            items = data.get("items", [])
            if items:
                channel_id = items[0]["snippet"]["channelId"]
                name = items[0]["snippet"]["title"]

    if channel_id:
        conn.execute(
            "INSERT OR REPLACE INTO channel_lookup (ref, channel_id, resolved_name) VALUES (?,?,?)",
            (cache_key, channel_id, name),
        )
        conn.commit()
    return channel_id, name


def ensure_uploads_playlist(conn, channel_id, name, niche):
    cur = conn.execute("SELECT uploads_playlist_id FROM channels WHERE channel_id=?", (channel_id,))
    row = cur.fetchone()
    if row and row[0]:
        conn.execute("UPDATE channels SET name=?, niche=? WHERE channel_id=?", (name, niche, channel_id))
        conn.commit()
        return row[0]
    data = yt_get("channels", {"part": "contentDetails", "id": channel_id})
    items = data.get("items", [])
    if not items:
        print(f"WARN: channel {channel_id} not found")
        return None
    uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    conn.execute(
        """INSERT INTO channels (channel_id, name, niche, uploads_playlist_id) VALUES (?,?,?,?)
           ON CONFLICT(channel_id) DO UPDATE SET
             name=excluded.name, niche=excluded.niche, uploads_playlist_id=excluded.uploads_playlist_id""",
        (channel_id, name, niche, uploads_id),
    )
    conn.commit()
    return uploads_id


def fetch_new_video_ids(uploads_playlist_id, known_ids, cutoff_iso=None):
    """Pages through a channel's uploads (newest first) and returns only the
    video IDs we haven't already got in the database. Stops as soon as it hits
    a video we already know about - since uploads are newest-first, everything
    after that point is already known too, so there's no need to keep paging.
    This keeps ongoing runs cheap no matter how big a channel's back catalogue is.
    If cutoff_iso is set (from a channel's 'since' date), it also stops there."""
    new_ids = []
    page_token = None
    while True:
        data = yt_get(
            "playlistItems",
            {"part": "contentDetails", "playlistId": uploads_playlist_id, "maxResults": 50, "pageToken": page_token},
        )
        stop = False
        for item in data.get("items", []):
            vid = item["contentDetails"]["videoId"]
            published_at = item["contentDetails"].get("videoPublishedAt")
            if vid in known_ids:
                stop = True
                break
            if cutoff_iso and published_at and published_at < cutoff_iso:
                stop = True
                break
            new_ids.append(vid)
        page_token = data.get("nextPageToken")
        if not page_token or stop:
            break
    return new_ids


def get_known_video_ids(conn, channel_id):
    """Every video we've ever seen for this channel, Shorts included - used only to
    know where to stop paging through the channel's upload playlist."""
    rows = conn.execute("SELECT video_id FROM videos WHERE channel_id=?", (channel_id,)).fetchall()
    return {r[0] for r in rows}


def get_trackable_video_ids(conn, channel_id):
    """Known videos we still want to refresh stats for - excludes anything already
    identified as a Short, so we stop spending quota re-checking videos we don't
    display anywhere."""
    if not EXCLUDE_SHORTS:
        return get_known_video_ids(conn, channel_id)
    rows = conn.execute(
        "SELECT video_id FROM videos WHERE channel_id=? AND is_short=0", (channel_id,)
    ).fetchall()
    return {r[0] for r in rows}


def fetch_stats(video_ids):
    results = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        if not batch:
            continue
        data = yt_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(batch)})
        for item in data.get("items", []):
            results[item["id"]] = item
    return results


def parse_dt(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def upsert_video(conn, channel_id, vid, info, is_short):
    snippet = info["snippet"]
    thumb = (snippet.get("thumbnails", {}).get("medium") or snippet.get("thumbnails", {}).get("default") or {}).get(
        "url", ""
    )
    conn.execute(
        """INSERT INTO videos (video_id, channel_id, title, published_at, thumbnail_url, is_short) VALUES (?,?,?,?,?,?)
           ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, thumbnail_url=excluded.thumbnail_url,
             is_short=excluded.is_short""",
        (vid, channel_id, snippet.get("title", ""), snippet.get("publishedAt"), thumb, int(is_short)),
    )


def record_snapshot(conn, vid, view_count, days_since_upload, now_iso):
    velocity = view_count / max(days_since_upload, 0.25)
    conn.execute(
        "INSERT INTO snapshots (video_id, checked_at, view_count, days_since_upload, velocity) VALUES (?,?,?,?,?)",
        (vid, now_iso, view_count, days_since_upload, velocity),
    )
    return velocity


def compute_baseline(conn, channel_id, video_id, days_since_upload):
    """Median velocity of the TRAILING_COMPARISON_VIDEOS other videos on this channel
    whose current age is closest to this video's - whichever side is nearer, published
    just before it or just after.

    This does NOT exclude videos currently flagged as outliers from the comparison
    pool. An earlier version did, on the theory that one hit shouldn't drag its own
    baseline up - but that turned out to be actively dangerous: excluding flagged
    videos from the pool, forever, meant that if a channel ever had a bad run where
    most of its videos got wrongly flagged (e.g. from the intra-run ordering bug this
    file used to have), every future run's baseline got computed from whatever tiny
    handful of videos were never flagged - almost always old, decayed, low-velocity
    ones - which made the *next* run's baseline artificially low too, re-flagging
    nearly everything again. That's a self-reinforcing trap a channel could never
    recover from on its own. Using the plain median instead is naturally robust to a
    minority of real outliers in the pool anyway (the median only moves if *more than
    half* the comparison videos are outliers, which at that point just reflects a
    genuine step-change in the channel's scale, not noise) - so there's no need for
    the exclusion, and removing it makes the whole system self-correcting instead of
    self-entrenching.

    This used to instead require a candidate to fall inside a fixed +/-40% age
    window, which could come up short of the required MIN_BASELINE_SAMPLES on a
    channel that's thin on history or was recently backfilled - even when there
    was perfectly good comparable data just outside that window. Sorting by
    nearness and taking the closest N instead means it always uses whatever's
    actually closest in time, and only gives up if the channel truly doesn't have
    enough tracked videos at all yet.

    Candidates are restricted to v2.is_short = 0 - videos the main loop currently
    treats as trackable. Without this, a handful of Shorts that happen to have a
    snapshot on record (e.g. from a run where duration data was briefly missing
    and a video got misclassified for one run before being corrected) could still
    show up here even though they're excluded everywhere else. Shorts naturally
    have tiny view counts, so if enough of them land near a video's age they can
    dominate the "nearest 15" pool and crater the baseline for real videos."""
    rows = conn.execute(
        """
        SELECT s.velocity, ABS(s.days_since_upload - ?) AS age_diff FROM (
            SELECT s2.video_id, MAX(s2.checked_at) AS latest_checked
            FROM snapshots s2
            JOIN videos v2 ON v2.video_id = s2.video_id
            WHERE v2.channel_id = ? AND s2.video_id != ? AND v2.is_short = 0
            GROUP BY s2.video_id
        ) latest
        JOIN snapshots s ON s.video_id = latest.video_id AND s.checked_at = latest.latest_checked
        ORDER BY age_diff ASC
        LIMIT ?
        """,
        (days_since_upload, channel_id, video_id, TRAILING_COMPARISON_VIDEOS),
    ).fetchall()
    velocities = [r[0] for r in rows]
    if len(velocities) < MIN_BASELINE_SAMPLES:
        return None
    return statistics.median(velocities)


def call_claude_digest(new_outliers):
    if not new_outliers:
        return None
    if ANTHROPIC_API_KEY.startswith("PLACEHOLDER"):
        print("Skipping Claude digest: ANTHROPIC_API_KEY not set yet")
        return None
    prompt = (
        "These YouTube videos just started outperforming their own channel's normal baseline. "
        "For each I give the title, channel, niche, and how many times its usual pace it's beating.\n"
        "Identify recurring title/hook/structural patterns across them, call out anything that shows up "
        "in more than one channel or niche, and note anything worth testing on other channels I run. "
        "Be concise and concrete - no generic advice.\n\n"
    )
    for v in new_outliers:
        prompt += f"- \"{v['title']}\" | channel: {v['channel_name']} | niche: {v['niche']} | {v['ratio']:.1f}x baseline\n"
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": CLAUDE_MODEL, "max_tokens": 600, "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        print(f"WARN: Claude digest call failed: {e}")
        return None


def send_telegram(text):
    if TELEGRAM_BOT_TOKEN.startswith("PLACEHOLDER") or TELEGRAM_CHAT_ID.startswith("PLACEHOLDER"):
        print("Skipping Telegram send: credentials not set yet. Message would have been:\n" + text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "disable_web_page_preview": True},
            timeout=30,
        )
    except Exception as e:
        print(f"WARN: Telegram send failed: {e}")


def resolve_all_channels(conn, entries):
    """Turns channels.yaml entries (URLs, handles, or raw IDs) into a clean list
    of {id, name, niche, since} dicts, resolving+caching each one against the API."""
    resolved = []
    for entry in entries:
        try:
            ref = parse_channel_ref(entry)
            if not ref:
                print(f"WARN: couldn't parse channel entry: {entry}")
                continue
            channel_id, resolved_name = resolve_channel_id(conn, ref)
            if not channel_id:
                print(f"WARN: couldn't resolve channel for: {entry}")
                continue
            name = entry.get("name") or resolved_name or channel_id
            niche = entry.get("niche", "uncategorized")
            resolved.append({"id": channel_id, "name": name, "niche": niche, "since": entry.get("since")})
        except Exception as e:
            print(f"WARN: error resolving channel entry {entry} - {e}")
        time.sleep(0.05)
    return resolved


def channel_cutoff(channel):
    """A channel can set 'since: YYYY-MM-DD' in channels.yaml to only track its
    videos from that date forward. Without it, there's no cutoff at all - the
    channel's full history gets tracked and kept up to date forever."""
    since = channel.get("since")
    if not since:
        return None
    try:
        d = datetime.datetime.strptime(str(since), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        print(f"WARN: couldn't parse 'since' date {since!r} for {channel.get('name')} - tracking full history")
        return None


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(DATA_JSON_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    entries = load_channel_entries()
    if not entries:
        print("No channels configured in channels.yaml yet - add some and re-run. Exiting.")
        conn.close()
        return

    channels = resolve_all_channels(conn, entries)
    if not channels:
        print("None of the channels in channels.yaml could be resolved. Check the URLs and re-run.")
        conn.close()
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    video_channel_map = {}
    all_video_ids = []
    total_new = 0

    for channel in channels:
        try:
            uploads_id = ensure_uploads_playlist(conn, channel["id"], channel["name"], channel["niche"])
            if not uploads_id:
                continue
            known_ids = get_known_video_ids(conn, channel["id"])
            new_ids = fetch_new_video_ids(uploads_id, known_ids, channel_cutoff(channel))
            total_new += len(new_ids)
            # Refresh every non-Short video this channel has ever had, not just what's
            # new this run. Already-classified Shorts are excluded here so we stop
            # spending quota on them; newly-discovered videos still get checked once
            # so we can find out whether they're Shorts in the first place.
            channel_video_ids = get_trackable_video_ids(conn, channel["id"]) | set(new_ids)
            for v in channel_video_ids:
                video_channel_map[v] = channel["id"]
            all_video_ids.extend(channel_video_ids)
        except Exception as e:
            # One problem channel (deleted, no uploads playlist, a transient API error,
            # etc.) should never take the whole run down - log it and move on.
            print(f"WARN: skipping channel {channel.get('name', channel.get('id'))} this run - {e}")
        time.sleep(0.05)

    all_video_ids = list(dict.fromkeys(all_video_ids))
    print(f"Refreshing {len(all_video_ids)} videos ({total_new} new) across {len(channels)} channels")

    stats = fetch_stats(all_video_ids)
    channel_names = {c["id"]: c["name"] for c in channels}
    channel_niches = {c["id"]: c["niche"] for c in channels}

    new_outliers = []
    dashboard_rows = []

    skipped_shorts = 0
    for vid, info in stats.items():
        channel_id = video_channel_map.get(vid)
        if not channel_id:
            continue

        duration_seconds = parse_duration_seconds(info.get("contentDetails", {}).get("duration"))
        is_short = bool(duration_seconds is not None and duration_seconds <= SHORTS_MAX_SECONDS)
        upsert_video(conn, channel_id, vid, info, is_short)

        if EXCLUDE_SHORTS and is_short:
            # Classified and stored (so we don't keep re-discovering it every run),
            # but it gets no snapshot, no baseline contribution, and no dashboard row.
            skipped_shorts += 1
            continue

        published_at = parse_dt(info["snippet"]["publishedAt"])
        days_since_upload = max((now - published_at).total_seconds() / 86400, 0)
        view_count = int(info.get("statistics", {}).get("viewCount", 0))
        velocity = record_snapshot(conn, vid, view_count, days_since_upload, now_iso)

        baseline = compute_baseline(conn, channel_id, vid, days_since_upload)
        ratio = (velocity / baseline) if baseline else None
        is_outlier = bool(baseline and ratio is not None and ratio >= OUTLIER_THRESHOLD)

        prev = conn.execute("SELECT is_outlier FROM outlier_state WHERE video_id=?", (vid,)).fetchone()
        was_outlier = bool(prev and prev[0])
        conn.execute(
            """INSERT INTO outlier_state (video_id, is_outlier, first_flagged_at) VALUES (?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 is_outlier=excluded.is_outlier,
                 first_flagged_at = CASE
                   WHEN excluded.is_outlier=1 AND outlier_state.is_outlier=0 THEN excluded.first_flagged_at
                   ELSE outlier_state.first_flagged_at
                 END""",
            (vid, int(is_outlier), now_iso if (is_outlier and not was_outlier) else None),
        )

        if is_outlier and not was_outlier:
            new_outliers.append(
                {
                    "video_id": vid,
                    "title": info["snippet"]["title"],
                    "channel_name": channel_names.get(channel_id, channel_id),
                    "niche": channel_niches.get(channel_id, "uncategorized"),
                    "ratio": ratio or 0,
                    "views": view_count,
                }
            )

        dashboard_rows.append(
            {
                "video_id": vid,
                "title": info["snippet"]["title"],
                "channel_id": channel_id,
                "channel_name": channel_names.get(channel_id, channel_id),
                "niche": channel_niches.get(channel_id, "uncategorized"),
                "thumbnail": (info["snippet"].get("thumbnails", {}).get("medium") or {}).get("url", ""),
                "published_at": info["snippet"]["publishedAt"],
                "views": view_count,
                "days_since_upload": round(days_since_upload, 1),
                "velocity": round(velocity, 1),
                "baseline": round(baseline, 1) if baseline else None,
                "ratio": round(ratio, 2) if ratio else None,
                "is_outlier": is_outlier,
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
        )

    conn.commit()

    dashboard_rows.sort(key=lambda r: r["published_at"], reverse=True)
    with open(DATA_JSON_PATH, "w") as f:
        json.dump(
            {
                "generated_at": now_iso,
                "videos": dashboard_rows,
                "channels": sorted(set(r["channel_name"] for r in dashboard_rows)),
                "niches": sorted(set(r["niche"] for r in dashboard_rows)),
                "channel_count": len(channels),
            },
            f,
            indent=2,
        )

    print(f"Skipped {skipped_shorts} Shorts this run (duration <= {SHORTS_MAX_SECONDS}s)")
    print(f"New outliers this run: {len(new_outliers)}")
    if new_outliers:
        digest = call_claude_digest(new_outliers)
        lines = [f"{len(new_outliers)} new outlier video(s) detected:\n"]
        for o in new_outliers:
            lines.append(f"- {o['title']} ({o['channel_name']}) - {o['ratio']:.1f}x baseline, {o['views']:,} views")
        if digest:
            lines.append("\nPattern digest:\n" + digest)
        send_telegram("\n".join(lines))

    conn.close()


if __name__ == "__main__":
    main()
