"""
Weekly title-suggestion generator.

Runs on its own schedule (.github/workflows/weekly_titles.yml), separately
from the main tracker (scripts/track.py). For each profile in profiles.yaml,
it gathers every video that was newly flagged as an outlier in the trailing
WEEKLY_LOOKBACK_DAYS-day window - matched to that profile by channel tag -
and asks Claude for a batch of title ideas written for that profile's own
channel, inspired by whatever's actually outperforming right now elsewhere
in the tracked list.

Pure read of data/tracker.db + profiles.yaml - it makes zero YouTube API
calls, so it costs no YouTube quota no matter how often it runs. It needs
ANTHROPIC_API_KEY; without it (or with no profiles configured yet) it exits
quietly, same as the main tracker's Claude digest.

Writes docs/title_suggestions.json, which the dashboard's "Suggestions" tab
reads directly - nothing else in the repo needs to change for this to show
up there.
"""

import os
import json
import sqlite3
import datetime

import requests
import yaml

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "PLACEHOLDER_ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-5"

# --- tunables -------------------------------------------------------------
WEEKLY_LOOKBACK_DAYS = 7        # how far back "this week's outliers" reaches
SUGGESTIONS_PER_PROFILE = 8     # how many titles to ask Claude for, per profile, per run
MAX_OUTLIERS_PER_PROFILE = 40   # caps how many outlier videos go into one Claude prompt, so a
                                 # broad "all" profile can't blow past a reasonable request size
# ---------------------------------------------------------------------------

ROOT = os.path.join(os.path.dirname(__file__), "..")
DB_PATH = os.path.join(ROOT, "data", "tracker.db")
PROFILES_YAML = os.path.join(ROOT, "profiles.yaml")
OUTPUT_PATH = os.path.join(ROOT, "docs", "title_suggestions.json")


def load_profiles():
    if not os.path.exists(PROFILES_YAML):
        return []
    with open(PROFILES_YAML) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg.get("profiles", []) or []


def profile_tags(p):
    return [str(t).strip() for t in (p.get("tags") or [])]


def matches_profile(profile_tag_list, video_tags):
    if "all" in profile_tag_list:
        return True
    return any(t in profile_tag_list for t in video_tags)


def channel_info_map(conn):
    """channel_id -> {name, tags}, reconstructed entirely from the local
    'channels' table (name, and 'niche' - a comma-joined tag string written
    by track.py's ensure_uploads_playlist every run). No need to touch
    channels.yaml or the YouTube API at all for this - the DB already has
    everything this script needs."""
    rows = conn.execute("SELECT channel_id, name, niche FROM channels").fetchall()
    return {
        cid: {"name": name, "tags": [t.strip() for t in (niche or "").split(",") if t.strip()]}
        for cid, name, niche in rows
    }


def recent_outliers(conn, cutoff_iso, channel_info):
    """Every video whose outlier_state.first_flagged_at falls within the
    lookback window, enriched with the channel name/tags and the
    ratio/view-count snapshot recorded at the moment it was flagged (not its
    current, possibly much higher, view count - this is "what stood out this
    week", not "what's biggest right now")."""
    rows = conn.execute(
        """
        SELECT v.video_id, v.title, v.channel_id, os.first_flagged_at, os.flagged_ratio, os.flagged_views
        FROM outlier_state os
        JOIN videos v ON v.video_id = os.video_id
        WHERE os.is_outlier = 1 AND os.first_flagged_at IS NOT NULL AND os.first_flagged_at >= ?
        ORDER BY os.flagged_ratio DESC
        """,
        (cutoff_iso,),
    ).fetchall()
    out = []
    for vid, title, channel_id, flagged_at, ratio, views in rows:
        info = channel_info.get(channel_id, {})
        out.append(
            {
                "video_id": vid,
                "title": title,
                "channel_id": channel_id,
                "channel_name": info.get("name", channel_id),
                "tags": info.get("tags", []),
                "flagged_at": flagged_at,
                "ratio": ratio,
                "views": views,
            }
        )
    return out


def call_claude_titles(profile, matched):
    name = profile.get("name", "Unnamed profile")
    if ANTHROPIC_API_KEY.startswith("PLACEHOLDER"):
        print(f"Skipping '{name}': ANTHROPIC_API_KEY not set yet")
        return None
    if not matched:
        print(f"Skipping '{name}': no matching outliers in the trailing {WEEKLY_LOOKBACK_DAYS} days")
        return None

    lines = []
    for o in matched[:MAX_OUTLIERS_PER_PROFILE]:
        ratio_txt = f"{o['ratio']:.1f}x baseline" if o["ratio"] is not None else "outlier"
        views_txt = f"{o['views']:,} views" if o["views"] is not None else "views n/a"
        tags_txt = ", ".join(o["tags"]) if o["tags"] else "uncategorized"
        lines.append(f"- \"{o['title']}\" | channel: {o['channel_name']} | tags: {tags_txt} | {ratio_txt} | {views_txt}")

    description = (profile.get("description") or "").strip()
    prompt = (
        f"Here is a written profile of a YouTube channel:\n\n{description}\n\n"
        f"Below is a list of videos from tracked channels that outperformed their own channel's "
        f"normal baseline in the past {WEEKLY_LOOKBACK_DAYS} days (title, channel, tags, how many "
        f"times over baseline, current views):\n\n"
        + "\n".join(lines)
        + f"\n\nBased on the title/hook/structural patterns in this list (not necessarily the exact "
        f"topics, unless genuinely relevant to this channel's niche), suggest {SUGGESTIONS_PER_PROFILE} "
        f"original video title ideas for THIS channel - titles its specific audience would click, "
        f"written in this channel's own voice. For each one, give the title and a short note on which "
        f"outlier(s) it draws from and why that underlying hook or structure should work here too. "
        f"Be concrete and specific, not generic - and don't suggest anything that's just a copy of an "
        f"existing title with the channel name swapped in."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": CLAUDE_MODEL, "max_tokens": 1800, "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    except Exception as e:
        print(f"WARN: Claude call failed for '{name}': {e}")
        return None


def write_output(generated_at, results):
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump({"generated_at": generated_at, "profiles": results}, f, indent=2)
    print(f"Wrote {OUTPUT_PATH}")


def main():
    profiles = load_profiles()
    if not profiles:
        print("No profiles configured in profiles.yaml yet - add some and re-run. Exiting.")
        if not os.path.exists(OUTPUT_PATH):
            write_output(None, [])
        return

    conn = sqlite3.connect(DB_PATH)
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_iso = (now - datetime.timedelta(days=WEEKLY_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")

    channel_info = channel_info_map(conn)
    outliers = recent_outliers(conn, cutoff_iso, channel_info)
    print(f"{len(outliers)} outlier(s) flagged in the trailing {WEEKLY_LOOKBACK_DAYS} days, across all channels")

    results = []
    for profile in profiles:
        name = profile.get("name", "Unnamed profile")
        tags = profile_tags(profile)
        matched = [o for o in outliers if matches_profile(tags, o["tags"])]
        print(f"Profile '{name}' (tags: {tags or ['none']}): {len(matched)} matching outlier(s)")
        suggestions_text = call_claude_titles(profile, matched)
        results.append(
            {
                "name": name,
                "tags": tags,
                "matched_outlier_count": len(matched),
                "suggestions": suggestions_text,
                "source_outliers": [
                    {
                        "title": o["title"],
                        "channel_name": o["channel_name"],
                        "ratio": round(o["ratio"], 2) if o["ratio"] is not None else None,
                        "views": o["views"],
                    }
                    for o in matched[:MAX_OUTLIERS_PER_PROFILE]
                ],
            }
        )

    conn.close()
    write_output(now_iso, results)


if __name__ == "__main__":
    main()
