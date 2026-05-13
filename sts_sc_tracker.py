#!/usr/bin/env python3
"""
STS & SC tracker — daily digest for the STS & SC niche.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
import requests

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ─────────────────────────────────────────────────────────────────────────────
# ADD OR REMOVE CHANNELS HERE — one @handle per line
# ─────────────────────────────────────────────────────────────────────────────
CHANNEL_HANDLES = [
    "@ExhaustTV93",
    "@simply-explained1",
    "@LogicMadeSimple100",
    "@TruckTropia",
    "@SimpleConceptsExplained",
    "@CasualNavigation",
    "@EngineScope256",
]
# ─────────────────────────────────────────────────────────────────────────────


def build_youtube():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def resolve_channel_id(youtube, handle: str) -> tuple[str, str]:
    response = youtube.search().list(
        part="snippet",
        q=handle,
        type="channel",
        maxResults=1,
    ).execute()
    items = response.get("items", [])
    if not items:
        raise ValueError(f"Could not resolve channel handle: {handle}")
    channel_id = items[0]["snippet"]["channelId"]
    title = items[0]["snippet"]["channelTitle"]
    return channel_id, title


def get_uploads_playlist_id(youtube, channel_id: str) -> str:
    response = youtube.channels().list(
        part="contentDetails",
        id=channel_id,
    ).execute()
    return response["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]


def get_playlist_videos(youtube, playlist_id: str, since: datetime) -> list[dict]:
    videos = []
    page_token = None

    while True:
        params = dict(part="snippet", playlistId=playlist_id, maxResults=50)
        if page_token:
            params["pageToken"] = page_token

        response = youtube.playlistItems().list(**params).execute()

        for item in response.get("items", []):
            snippet = item["snippet"]
            published_at_str = snippet.get("publishedAt", "")
            if not published_at_str:
                continue
            published_at = datetime.fromisoformat(published_at_str.replace("Z", "+00:00"))
            if published_at < since:
                return videos
            video_id = snippet["resourceId"]["videoId"]
            videos.append({
                "video_id": video_id,
                "title": snippet["title"],
                "channel": snippet["channelTitle"],
                "published_at": published_at,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return videos


def enrich_with_stats(youtube, videos: list[dict]) -> list[dict]:
    if not videos:
        return videos

    enriched = []
    ids = [v["video_id"] for v in videos]

    for i in range(0, len(ids), 50):
        batch = ids[i : i + 50]
        response = youtube.videos().list(
            part="statistics",
            id=",".join(batch),
        ).execute()

        stats_map = {
            item["id"]: int(item["statistics"].get("viewCount", 0))
            for item in response.get("items", [])
        }
        for v in videos[i : i + 50]:
            v["view_count"] = stats_map.get(v["video_id"], 0)
            enriched.append(v)

    return enriched


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def to_et(dt: datetime) -> tuple[datetime, str]:
    offset_hours = -4 if 3 <= dt.month <= 11 else -5
    label = "EDT" if offset_hours == -4 else "EST"
    return dt.astimezone(timezone(timedelta(hours=offset_hours))), label


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(all_videos: list[dict], now: datetime) -> str:
    cutoff_24h = now - timedelta(hours=24)
    videos_24h = [v for v in all_videos if v["published_at"] >= cutoff_24h]

    now_et, et_label = to_et(now)

    lines = []
    lines.append("\U0001f6a2 <b>STS &amp; SC — Daily Channel Report</b>")
    lines.append(f"<i>{now_et.strftime('%A, %B %-d %Y — %I:%M %p')} {et_label}</i>")
    lines.append(f"<i>Tracking {len(CHANNEL_HANDLES)} channels</i>")
    lines.append("")

    lines.append("\U0001f195 <b>New Videos (Last 24 Hours)</b>")
    if videos_24h:
        for v in sorted(videos_24h, key=lambda x: x["published_at"], reverse=True):
            pub_et, pub_label = to_et(v["published_at"])
            pub_str = pub_et.strftime("%-I:%M %p") + f" {pub_label}"
            lines.append(
                f'• <a href="{v["url"]}">{esc(v["title"])}</a>\n'
                f'  <i>{esc(v["channel"])}</i> · Published {pub_str} · \U0001f441 {format_number(v["view_count"])}'
            )
    else:
        lines.append("<i>No new videos in the last 24 hours.</i>")
    lines.append("")

    lines.append("\U0001f4c8 <b>Top 3 by Views — Last 24 Hours</b>")
    top_24h = sorted(videos_24h, key=lambda x: x["view_count"], reverse=True)[:3]
    if top_24h:
        for i, v in enumerate(top_24h, 1):
            lines.append(
                f'{i}. <a href="{v["url"]}">{esc(v["title"])}</a>\n'
                f'   \U0001f441 {format_number(v["view_count"])} · <i>{esc(v["channel"])}</i>'
            )
    else:
        lines.append("<i>No data for this period.</i>")

    return "\n".join(lines)


def send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=30)
    if not response.ok:
        print(f"Telegram API error {response.status_code}: {response.text}", file=sys.stderr)
        response.raise_for_status()
    print("Telegram message sent successfully.")


def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)

    youtube = build_youtube()
    all_videos = []

    for handle in CHANNEL_HANDLES:
        print(f"Fetching {handle}…")
        try:
            channel_id, _ = resolve_channel_id(youtube, handle)
            playlist_id = get_uploads_playlist_id(youtube, channel_id)
            videos = get_playlist_videos(youtube, playlist_id, since=cutoff)
            all_videos.extend(videos)
            print(f"  → {len(videos)} videos found")
        except Exception as exc:
            print(f"  WARNING: failed to fetch {handle}: {exc}", file=sys.stderr)

    print(f"Enriching {len(all_videos)} videos with stats…")
    all_videos = enrich_with_stats(youtube, all_videos)

    message = build_message(all_videos, now)
    print("Sending Telegram message…")
    send_telegram(message)


if __name__ == "__main__":
    main()
