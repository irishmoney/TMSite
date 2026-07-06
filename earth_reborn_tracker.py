#!/usr/bin/env python3
"""
Earth Reborn tracker — per-channel daily digest.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
import requests

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

REPORT_TITLE = "\U0001f30d EARTH REBORN — Daily Channel Report"

# ─────────────────────────────────────────────────────────────────────────────
# ADD OR REMOVE CHANNELS HERE — one @handle per line
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL_HANDLES = [
    "@DailyDiscoveriesOff",
    "@MakeTechFuture",
    "@NatureRebuilt",
    "@TheEnkiCodex",
    "@GenesisChamberUS",
    "@TheOriginVaultOfficial",
    "@20_percent",
    "@LeaveCurious",
    "@WildRevivalYT",
    "@AgricultureFlow",
    "@treeline_journal",
]
# ─────────────────────────────────────────────────────────────────────────────


def build_youtube():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def resolve_channel_id(youtube, handle):
    response = youtube.search().list(part="snippet", q=handle, type="channel", maxResults=1).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError(f"Could not resolve channel handle: {handle}")
    return items[0]["snippet"]["channelId"], items[0]["snippet"]["channelTitle"]


def get_uploads_playlist_id(youtube, channel_id):
    response = youtube.channels().list(part="contentDetails", id=channel_id).execute()
    return response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_channel_videos(youtube, playlist_id, since_48h):
    latest = None
    videos_48h = []
    page_token = None

    while True:
        params = dict(part="snippet", playlistId=playlist_id, maxResults=50)
        if page_token:
            params["pageToken"] = page_token
        response = youtube.playlistItems().list(**params).execute()

        for item in response.get("items", []):
            snippet = item["snippet"]
            pub_str = snippet.get("publishedAt", "")
            if not pub_str:
                continue
            published_at = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            video_id = snippet["resourceId"]["videoId"]
            video = {
                "video_id": video_id,
                "title": snippet["title"],
                "channel": snippet["channelTitle"],
                "published_at": published_at,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "view_count": 0,
            }
            if latest is None:
                latest = video
            if published_at >= since_48h:
                videos_48h.append(video)
            else:
                return latest, videos_48h

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return latest, videos_48h


def enrich_with_stats(youtube, videos):
    if not videos:
        return
    ids = [v["video_id"] for v in videos]
    for i in range(0, len(ids), 50):
        batch = ids[i: i + 50]
        response = youtube.videos().list(part="statistics", id=",".join(batch)).execute()
        stats_map = {
            item["id"]: int(item["statistics"].get("viewCount", 0))
            for item in response.get("items", [])
        }
        for v in videos[i: i + 50]:
            v["view_count"] = stats_map.get(v["video_id"], 0)


def vph(v, now):
    hours = max((now - v["published_at"]).total_seconds() / 3600, 0.5)
    return v["view_count"] / hours


def fmt(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def to_et(dt):
    offset_hours = -4 if 3 <= dt.month <= 11 else -5
    label = "EDT" if offset_hours == -4 else "EST"
    return dt.astimezone(timezone(timedelta(hours=offset_hours))), label


def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def video_line(v, now):
    pub_et, pub_label = to_et(v["published_at"])
    pub_str = pub_et.strftime("%-I:%M %p") + f" {pub_label}"
    return (
        f'<a href="{v["url"]}">{esc(v["title"])}</a>\n'
        f'   {pub_str} · \U0001f441 {fmt(v["view_count"])} · ⚡ {fmt(vph(v, now))}/hr'
    )


def build_message(channel_data, now):
    now_et, et_label = to_et(now)

    lines = []
    lines.append(f"<b>{REPORT_TITLE}</b>")
    lines.append(f"<i>{now_et.strftime('%A, %B %-d %Y — %I:%M %p')} {et_label}</i>")
    lines.append(f"<i>Tracking {len(CHANNEL_HANDLES)} channels</i>")

    for entry in channel_data:
        name = entry["name"]
        latest = entry["latest"]
        best = entry["best_48h"]

        lines.append("")
        lines.append(f"━━━ <b>{esc(name)}</b> ━━━")

        if latest is None:
            lines.append("<i>Could not fetch channel data.</i>")
            continue

        lines.append(f"\U0001f4cc <b>Latest:</b> {video_line(latest, now)}")

        if best and best["video_id"] != latest["video_id"]:
            lines.append(f"\U0001f3c6 <b>Best 48h:</b> {video_line(best, now)}")
        elif best is None:
            lines.append("<i>No new videos in last 48h</i>")

    return "\n".join(lines)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    response = requests.post(url, json=payload, timeout=30)
    if not response.ok:
        print(f"Telegram API error {response.status_code}: {response.text}", file=sys.stderr)
        response.raise_for_status()
    print("Telegram message sent successfully.")


def main():
    now = datetime.now(timezone.utc)
    since_48h = now - timedelta(hours=48)
    youtube = build_youtube()
    channel_data = []

    for handle in CHANNEL_HANDLES:
        print(f"Fetching {handle}…")
        try:
            channel_id, channel_name = resolve_channel_id(youtube, handle)
            playlist_id = get_uploads_playlist_id(youtube, channel_id)
            latest, videos_48h = get_channel_videos(youtube, playlist_id, since_48h)

            to_enrich = {v["video_id"]: v for v in videos_48h}
            if latest:
                to_enrich[latest["video_id"]] = latest
            enrich_with_stats(youtube, list(to_enrich.values()))

            best_48h = max(videos_48h, key=lambda v: v["view_count"]) if videos_48h else None

            channel_data.append({
                "name": channel_name,
                "latest": latest,
                "best_48h": best_48h,
            })
            print(f"  → latest: {latest['title'][:50] if latest else 'none'}, 48h videos: {len(videos_48h)}")
        except Exception as exc:
            print(f"  WARNING: failed to fetch {handle}: {exc}", file=sys.stderr)
            channel_data.append({"name": handle, "latest": None, "best_48h": None})

    message = build_message(channel_data, now)
    print("Sending Telegram message…")
    send_telegram(message)


if __name__ == "__main__":
    main()
