import os
import json
import time
import tempfile
import datetime
import statistics
from pathlib import Path
from flask import Flask, jsonify, redirect, request, session, render_template, url_for
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# ── Config ──────────────────────────────────────────────────────────────────
# On Railway: set YT_CLIENT_SECRET_JSON env var to the full contents of client_secret.json
# Locally: set YT_CLIENT_SECRET to the file path (falls back to ~/Desktop/client_secret.json)
_CLIENT_SECRET_JSON = os.environ.get("YT_CLIENT_SECRET_JSON")
_CLIENT_SECRET_FILE = os.environ.get(
    "YT_CLIENT_SECRET",
    os.path.expanduser("~/Desktop/client_secret.json"),
)

CACHE_DIR = Path(__file__).parent / "cache"
TOKEN_FILE = CACHE_DIR / "token.json"
CACHE_FILE = CACHE_DIR / "dashboard_data.json"
CACHE_TTL_HOURS = 24

# On Railway the app is behind a TLS-terminating proxy — trust X-Forwarded-Proto
RAILWAY = os.environ.get("RAILWAY_ENVIRONMENT") is not None
if RAILWAY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
else:
    # Allow OAuth over plain http for local development
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


# ── Credentials file helper ───────────────────────────────────────────────────
def _client_secret_file():
    """Return path to a client_secret.json, writing a temp file if loaded from env."""
    if _CLIENT_SECRET_JSON:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix="yt_secret_"
        )
        tmp.write(_CLIENT_SECRET_JSON)
        tmp.flush()
        return tmp.name
    return _CLIENT_SECRET_FILE


# ── Auth helpers ─────────────────────────────────────────────────────────────
def get_credentials():
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
            return creds
    return None


def _save_token(creds):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json())


def _redirect_uri():
    return url_for("auth_callback", _external=True)


# ── Cache helpers ─────────────────────────────────────────────────────────────
def cache_valid():
    if not CACHE_FILE.exists():
        return False
    age_hours = (time.time() - CACHE_FILE.stat().st_mtime) / 3600
    return age_hours < CACHE_TTL_HOURS


def load_cache():
    return json.loads(CACHE_FILE.read_text())


def save_cache(data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["cached_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    CACHE_FILE.write_text(json.dumps(data, indent=2))


# ── YouTube data fetching ─────────────────────────────────────────────────────
def fetch_all_data():
    creds = get_credentials()
    yt = build("youtube", "v3", credentials=creds)
    yta = build("youtubeAnalytics", "v2", credentials=creds)

    # 1. Channel overview
    ch_resp = yt.channels().list(part="snippet,statistics", mine=True).execute()
    channel = ch_resp["items"][0]
    channel_id = channel["id"]
    stats = channel["statistics"]
    overview = {
        "channel_id": channel_id,
        "title": channel["snippet"]["title"],
        "subscribers": int(stats.get("subscriberCount", 0)),
        "total_views": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
    }

    # 2. Watch time (last 90 days from Analytics API)
    today = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    wt_resp = yta.reports().query(
        ids=f"channel=={channel_id}",
        startDate=start,
        endDate=today,
        metrics="estimatedMinutesWatched,views",
    ).execute()
    rows = wt_resp.get("rows", [[0, 0]])
    overview["watch_time_minutes"] = int(rows[0][0]) if rows else 0

    # 3. All uploads
    uploads_id = _get_uploads_playlist(yt, channel_id)
    video_ids = _get_all_video_ids(yt, uploads_id)

    # 4. Video details (in batches of 50)
    videos = _get_video_details(yt, video_ids)

    # 5. Analytics per video (CTR, AVD, impressions)
    videos = _enrich_with_analytics(yta, channel_id, videos)

    # 6. Comments from last 20 videos (sorted by publish date)
    sorted_vids = sorted(videos, key=lambda v: v.get("published_at", ""), reverse=True)
    comments = _get_comments(yt, [v["id"] for v in sorted_vids[:20]])

    # 7. Compute outlier flags
    videos = _flag_outliers(videos)

    # 8. AI insights (algorithmic analysis)
    insights = _generate_insights(overview, videos)

    return {
        "overview": overview,
        "videos": videos,
        "comments": comments,
        "insights": insights,
    }


def _get_uploads_playlist(yt, channel_id):
    resp = yt.channels().list(part="contentDetails", id=channel_id).execute()
    return resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]


def _get_all_video_ids(yt, playlist_id):
    ids = []
    page_token = None
    while True:
        resp = yt.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        ids.extend(item["contentDetails"]["videoId"] for item in resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def _get_video_details(yt, video_ids):
    videos = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = yt.videos().list(
            part="snippet,statistics,contentDetails",
            id=",".join(batch),
        ).execute()
        for item in resp.get("items", []):
            s = item["statistics"]
            sn = item["snippet"]
            cd = item["contentDetails"]
            videos.append(
                {
                    "id": item["id"],
                    "title": sn.get("title", ""),
                    "published_at": sn.get("publishedAt", ""),
                    "thumbnail": sn.get("thumbnails", {}).get("medium", {}).get("url", ""),
                    "views": int(s.get("viewCount", 0)),
                    "likes": int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                    "duration": _parse_duration(cd.get("duration", "PT0S")),
                    "ctr": None,
                    "avd_seconds": None,
                    "impressions": None,
                }
            )
    return videos


def _enrich_with_analytics(yta, channel_id, videos):
    today = datetime.date.today().isoformat()
    start = (datetime.date.today() - datetime.timedelta(days=730)).isoformat()
    id_map = {v["id"]: v for v in videos}
    for vid_id in list(id_map.keys()):
        try:
            resp = yta.reports().query(
                ids=f"channel=={channel_id}",
                startDate=start,
                endDate=today,
                metrics="impressions,impressionClickThroughRate,averageViewDuration",
                filters=f"video=={vid_id}",
            ).execute()
            rows = resp.get("rows")
            if rows:
                row = rows[0]
                id_map[vid_id]["impressions"] = int(row[0])
                id_map[vid_id]["ctr"] = round(float(row[1]) * 100, 2)
                id_map[vid_id]["avd_seconds"] = int(row[2])
        except Exception:
            pass
    return list(id_map.values())


def _get_comments(yt, video_ids):
    comments_by_video = {}
    for vid_id in video_ids:
        try:
            resp = yt.commentThreads().list(
                part="snippet",
                videoId=vid_id,
                maxResults=10,
                order="relevance",
                textFormat="plainText",
            ).execute()
            comments_by_video[vid_id] = [
                {
                    "author": item["snippet"]["topLevelComment"]["snippet"]["authorDisplayName"],
                    "text": item["snippet"]["topLevelComment"]["snippet"]["textDisplay"],
                    "likes": item["snippet"]["topLevelComment"]["snippet"]["likeCount"],
                    "published_at": item["snippet"]["topLevelComment"]["snippet"]["publishedAt"],
                }
                for item in resp.get("items", [])
            ]
        except Exception:
            comments_by_video[vid_id] = []
    return comments_by_video


def _flag_outliers(videos):
    views_list = [v["views"] for v in videos if v["views"] > 0]
    avd_list = [v["avd_seconds"] for v in videos if v.get("avd_seconds")]
    ctr_list = [v["ctr"] for v in videos if v.get("ctr") is not None]

    def stats(lst):
        if len(lst) < 2:
            return 0, float("inf")
        return statistics.mean(lst), statistics.stdev(lst)

    v_mean, v_std = stats(views_list)
    a_mean, a_std = stats(avd_list)
    c_mean, c_std = stats(ctr_list)

    for v in videos:
        score = 0
        if v_std and v["views"]:
            score += (v["views"] - v_mean) / v_std
        if a_std and v.get("avd_seconds"):
            score += (v["avd_seconds"] - a_mean) / a_std
        if c_std and v.get("ctr") is not None:
            score += (v["ctr"] - c_mean) / c_std
        v["outlier_score"] = round(score, 2)
        if score > 1.5:
            v["outlier_label"] = "overperforming"
        elif score < -1.5:
            v["outlier_label"] = "underperforming"
        else:
            v["outlier_label"] = "normal"

    return videos


def _generate_insights(overview, videos):
    insights = []
    views_list = [v["views"] for v in videos if v["views"] > 0]
    avd_list = [v["avd_seconds"] for v in videos if v.get("avd_seconds")]
    ctr_list = [v["ctr"] for v in videos if v.get("ctr") is not None]

    if views_list:
        avg_views = statistics.mean(views_list)
        top = [v for v in videos if v["views"] > avg_views * 2]
        if top:
            titles = ", ".join(f'"{v["title"]}"' for v in top[:3])
            insights.append({
                "type": "success",
                "title": "Top Outlier Videos",
                "body": f"{len(top)} video(s) are getting 2× the channel average in views: {titles}. Study their titles, thumbnails, and topics for patterns.",
            })

    if avd_list:
        avg_avd = statistics.mean(avd_list)
        minutes = int(avg_avd // 60)
        secs = int(avg_avd % 60)
        low_avd = [v for v in videos if v.get("avd_seconds") and v["avd_seconds"] < avg_avd * 0.7]
        insights.append({
            "type": "info",
            "title": "Average View Duration",
            "body": f"Channel average AVD is {minutes}m {secs}s. {len(low_avd)} video(s) are significantly below this — consider reviewing their hooks and pacing.",
        })

    if ctr_list:
        avg_ctr = statistics.mean(ctr_list)
        low_ctr = [v for v in videos if v.get("ctr") is not None and v["ctr"] < avg_ctr * 0.6]
        high_ctr = [v for v in videos if v.get("ctr") is not None and v["ctr"] > avg_ctr * 1.5]
        insights.append({
            "type": "info",
            "title": "Click-Through Rate",
            "body": f"Average CTR is {avg_ctr:.1f}%. {len(high_ctr)} video(s) have strong CTR — their thumbnails/titles are resonating. {len(low_ctr)} video(s) have weak CTR and could benefit from thumbnail or title A/B testing.",
        })

    if views_list and avd_list:
        bait = [
            v for v in videos
            if v["views"] > statistics.mean(views_list)
            and v.get("avd_seconds")
            and v["avd_seconds"] < statistics.mean(avd_list) * 0.75
        ]
        if bait:
            insights.append({
                "type": "warning",
                "title": "Possible Clickbait Risk",
                "body": f"{len(bait)} video(s) get above-average clicks but below-average watch time. This signals the thumbnail/title may be overpromising. Fix the hook or align expectations.",
            })

    over = [v for v in videos if v.get("outlier_label") == "overperforming"]
    under = [v for v in videos if v.get("outlier_label") == "underperforming"]
    if over:
        insights.append({
            "type": "success",
            "title": f"{len(over)} Overperforming Video(s)",
            "body": "These videos are outperforming the channel average across views, AVD, and CTR. Double down on these formats, topics, or styles.",
        })
    if under:
        insights.append({
            "type": "warning",
            "title": f"{len(under)} Underperforming Video(s)",
            "body": "These videos consistently underperform. Analyze what's different — topic, length, publish time, or thumbnail — and avoid repeating those patterns.",
        })

    return insights


def _parse_duration(iso):
    import re
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/auth/login")
def auth_login():
    secret_file = _client_secret_file()
    flow = Flow.from_client_secrets_file(
        secret_file,
        scopes=SCOPES,
        redirect_uri=_redirect_uri(),
    )
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true")
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    secret_file = _client_secret_file()
    flow = Flow.from_client_secrets_file(
        secret_file,
        scopes=SCOPES,
        state=session.get("oauth_state"),
        redirect_uri=_redirect_uri(),
    )
    # On Railway the callback URL will be https:// but Flask may see http://
    callback_url = request.url.replace("http://", "https://") if RAILWAY else request.url
    flow.fetch_token(authorization_response=callback_url)
    _save_token(flow.credentials)
    return redirect(url_for("index"))


@app.route("/api/status")
def api_status():
    creds = get_credentials()
    return jsonify({"authenticated": creds is not None, "cache_valid": cache_valid()})


@app.route("/api/data")
def api_data():
    refresh = request.args.get("refresh") == "1"
    if not refresh and cache_valid():
        return jsonify(load_cache())
    creds = get_credentials()
    if not creds:
        return jsonify({"error": "not_authenticated"}), 401
    try:
        data = fetch_all_data()
        save_cache(data)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/logout")
def api_logout():
    if TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
    CACHE_FILE.unlink(missing_ok=True)
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=not RAILWAY)
