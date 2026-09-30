#!/usr/bin/env python3
"""Fetch YouTube stats for one channel and write data/latest.json + data/history.json.

Standard library only. Needs env var YT_API_KEY (YouTube Data API v3 key).
Optional: CHANNEL_HANDLE (default @currentconcept), FULL_REFRESH=1 to refetch every video's comments.
Quota per run is roughly 1 + 2*ceil(N/50) + (videos whose comments get refreshed) units,
far below the free 10,000/day.
"""
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.googleapis.com/youtube/v3/"
KEY = os.environ.get("YT_API_KEY")
HANDLE = os.environ.get("CHANNEL_HANDLE", "@currentconcept")
DATA = Path(__file__).resolve().parent.parent / "data"
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
# Comments on recent videos change fast; older ones are refreshed once a week (Sundays).
RECENT_DAYS = 45
FULL_COMMENT_REFRESH = NOW.weekday() == 6 or os.environ.get("FULL_REFRESH") == "1"


def api(endpoint, **params):
    params["key"] = KEY
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def iso_duration(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def load(name, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def top_comments(video_id):
    """Top-level comments by relevance; keeps the 3 most-liked of the first 100."""
    try:
        res = api("commentThreads", part="snippet", videoId=video_id, maxResults=100,
                  order="relevance", textFormat="plainText")
    except urllib.error.HTTPError as e:  # comments disabled, members-only, etc.
        print(f"  comments unavailable for {video_id}: {e.code}", file=sys.stderr)
        return []
    out = []
    for item in res.get("items", []):
        s = item["snippet"]["topLevelComment"]["snippet"]
        out.append({
            "author": s.get("authorDisplayName", ""),
            "text": s.get("textOriginal", s.get("textDisplay", ""))[:1200],
            "likes": s.get("likeCount", 0),
            "replies": item["snippet"].get("totalReplyCount", 0),
            "published": s.get("publishedAt"),
        })
    out.sort(key=lambda c: c["likes"], reverse=True)
    return out[:3]


def main():
    if not KEY:
        sys.exit("YT_API_KEY is not set")
    DATA.mkdir(exist_ok=True)

    res = api("channels", part="snippet,statistics,contentDetails", forHandle=HANDLE.lstrip("@"))
    if not res.get("items"):
        sys.exit(f"No channel found for handle {HANDLE}")
    ch = res["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]

    ids, token = [], None
    while True:
        params = dict(part="contentDetails", playlistId=uploads, maxResults=50)
        if token:
            params["pageToken"] = token
        page = api("playlistItems", **params)
        ids += [i["contentDetails"]["videoId"] for i in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break

    prev = {v["id"]: v for v in load("latest.json", {}).get("videos", [])}
    videos = []
    for i in range(0, len(ids), 50):
        page = api("videos", part="snippet,statistics,contentDetails,liveStreamingDetails",
                   id=",".join(ids[i:i + 50]))
        for v in page.get("items", []):
            sn, st, cd = v["snippet"], v.get("statistics", {}), v["contentDetails"]
            if sn.get("liveBroadcastContent") in ("live", "upcoming"):
                continue
            published = sn["publishedAt"]
            age = (NOW - dt.datetime.fromisoformat(published.replace("Z", "+00:00"))).days
            old = prev.get(v["id"], {})
            refresh = FULL_COMMENT_REFRESH or age <= RECENT_DAYS or "top_comments" not in old
            thumbs = sn.get("thumbnails", {})
            videos.append({
                "id": v["id"],
                "title": sn["title"],
                "published": published,
                "duration": iso_duration(cd.get("duration")),
                "views": int(st.get("viewCount", 0)),
                "likes": int(st["likeCount"]) if "likeCount" in st else None,
                "comments": int(st["commentCount"]) if "commentCount" in st else None,
                "thumb": (thumbs.get("medium") or thumbs.get("default") or {}).get("url", ""),
                "tags": sn.get("tags", [])[:30],
                "was_live": "liveStreamingDetails" in v,
                "top_comments": top_comments(v["id"]) if refresh else old.get("top_comments", []),
                "comments_checked": TODAY if refresh else old.get("comments_checked"),
            })

    videos.sort(key=lambda v: v["published"])
    s = ch["statistics"]
    thumbs = ch["snippet"].get("thumbnails", {})
    channel = {
        "id": ch["id"],
        "handle": HANDLE,
        "title": ch["snippet"]["title"],
        "avatar": (thumbs.get("high") or thumbs.get("default") or {}).get("url", ""),
        "created": ch["snippet"].get("publishedAt"),
        "subscribers": int(s.get("subscriberCount", 0)),
        "subscribers_hidden": s.get("hiddenSubscriberCount", False),
        "views": int(s.get("viewCount", 0)),
        "video_count": int(s.get("videoCount", 0)),
    }

    latest = {"generated_at": NOW.isoformat(timespec="seconds"), "channel": channel, "videos": videos}
    (DATA / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    # History: one row per day; re-running on the same day replaces that day's row.
    hist = load("history.json", {"channel": [], "videos": {}})
    hist["channel"] = [r for r in hist["channel"] if r[0] != TODAY]
    hist["channel"].append([TODAY, channel["subscribers"], channel["views"], len(videos)])
    for v in videos:
        rows = [r for r in hist["videos"].get(v["id"], []) if r[0] != TODAY]
        rows.append([TODAY, v["views"], v["likes"], v["comments"]])
        hist["videos"][v["id"]] = rows
    (DATA / "history.json").write_text(json.dumps(hist, separators=(",", ":")), encoding="utf-8")

    refreshed = sum(v["comments_checked"] == TODAY for v in videos)
    print(f"{channel['title']}: {len(videos)} videos, {channel['subscribers']} subs, "
          f"comments refreshed on {refreshed} videos")


if __name__ == "__main__":
    main()
