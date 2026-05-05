#!/usr/bin/env python3
"""
YouTube channel tracker — fetches recent videos and sends a daily Telegram digest.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from googleapiclient.discovery import build
import requests

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CHANNEL_HANDLES = [
    "@CrisisRadar1",
    "@LorenzosDisasterForecast",
    "@brokenearthyt",
    "@naturaldisastersuncovered",
]


def build_youtube():
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def resolve_channel_id(youtube, handle: str) -> tuple[str, str]:
    """Return (channel_id, display_name) for a @handle."""
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
    """Fetch all video stubs from a playlist published after `since`."""
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
                return videos  # playlist is newest-first; stop early
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


def enrich_with_view_counts(youtube, videos: list[dict]) -> list[dict]:
    """Add view_count to each video dict (batch requests of 50)."""
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


def format_time_ago(published_at: datetime, now: datetime) -> str:
    delta = now - published_at
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes}m ago"
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def esc(text: str) -> str:
    """Escape special HTML characters in dynamic content."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(all_videos: list[dict], now: datetime) -> str:
    cutoff_24h = now - timedelta(hours=24)
    cutoff_48h = now - timedelta(hours=48)
    cutoff_7d = now - timedelta(days=7)

    videos_24h = [v for v in all_videos if v["published_at"] >= cutoff_24h]
    videos_48h = [v for v in all_videos if v["published_at"] >= cutoff_48h]
    videos_7d = [v for v in all_videos if v["published_at"] >= cutoff_7d]

    lines = []
    lines.append("📺 <b>YouTube Disaster Channel Daily Report</b>")
    lines.append(f"<i>{now.strftime('%A, %B %-d %Y — %I:%M %p UTC')}</i>")
    lines.append("")

    # ── Section 1: New videos in last 24 h ─────────────────────────────────
    lines.append("🆕 <b>New Videos (Last 24 Hours)</b>")
    if videos_24h:
        for v in sorted(videos_24h, key=lambda x: x["published_at"], reverse=True):
            ago = format_time_ago(v["published_at"], now)
            lines.append(
                f'• <a href="{v["url"]}">{esc(v["title"])}</a>\n'
                f'  <i>{esc(v["channel"])}</i> · {ago}'
            )
    else:
        lines.append("<i>No new videos in the last 24 hours.</i>")
    lines.append("")

    # ── Section 2: Top 3 by views — 24 h ───────────────────────────────────
    lines.append("📈 <b>Top 3 by Views — Last 24 Hours</b>")
    top_24h = sorted(videos_24h, key=lambda x: x["view_count"], reverse=True)[:3]
    if top_24h:
        for i, v in enumerate(top_24h, 1):
            lines.append(
                f'{i}. <a href="{v["url"]}">{esc(v["title"])}</a>\n'
                f'   👁 {format_number(v["view_count"])} · <i>{esc(v["channel"])}</i>'
            )
    else:
        lines.append("<i>No data for this period.</i>")
    lines.append("")

    # ── Section 3: Top 3 by views — 48 h ───────────────────────────────────
    lines.append("📊 <b>Top 3 by Views — Last 48 Hours</b>")
    top_48h = sorted(videos_48h, key=lambda x: x["view_count"], reverse=True)[:3]
    if top_48h:
        for i, v in enumerate(top_48h, 1):
            lines.append(
                f'{i}. <a href="{v["url"]}">{esc(v["title"])}</a>\n'
                f'   👁 {format_number(v["view_count"])} · <i>{esc(v["channel"])}</i>'
            )
    else:
        lines.append("<i>No data for this period.</i>")
    lines.append("")

    # ── Section 4: Top 3 by views — 7 days ─────────────────────────────────
    lines.append("🏆 <b>Top 3 by Views — Last 7 Days</b>")
    top_7d = sorted(videos_7d, key=lambda x: x["view_count"], reverse=True)[:3]
    if top_7d:
        for i, v in enumerate(top_7d, 1):
            lines.append(
                f'{i}. <a href="{v["url"]}">{esc(v["title"])}</a>\n'
                f'   👁 {format_number(v["view_count"])} · <i>{esc(v["channel"])}</i>'
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
    cutoff = now - timedelta(days=7)  # fetch up to 7 days back

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

    print(f"Enriching {len(all_videos)} videos with view counts…")
    all_videos = enrich_with_view_counts(youtube, all_videos)

    # Convert UTC time to EST for the message header (UTC-5, no DST adjustment needed
    # since GitHub Actions scheduler is consistent; use a fixed offset for simplicity)
    est_offset = timedelta(hours=-5)
    now_est = now + est_offset
    # Rebuild a timezone-aware datetime for display only
    est_tz = timezone(est_offset)
    now_est = now.astimezone(est_tz)

    message = build_message(all_videos, now)
    print("Sending Telegram message…")
    send_telegram(message)


if __name__ == "__main__":
    main()
