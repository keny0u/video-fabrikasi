"""Yayınlanan bölümün yorumlarından A/B oylarını sayar; her kişinin yalnızca son oyu geçerlidir."""
import re

import requests

TURKISH_LETTERS = "a-zçğıöşü0-9"
VOTE = re.compile(rf"(?<![{TURKISH_LETTERS}])([ab])(?![{TURKISH_LETTERS}])")
EMOJI = {"🅰": "a", "🅱": "b"}
YOUTUBE_ID = re.compile(r"(?:youtu\.be/|v=|shorts/)([\w-]{11})")
MAX_PAGES = 10


def parse_vote(text):
    """Yorumdaki oy: tek başına "A" ya da "B" (ör. "A olsun", "bence b"). İkisi birden geçiyorsa oy sayılmaz."""
    lowered = text.replace("İ", "i").casefold()
    for emoji, letter in EMOJI.items():
        lowered = lowered.replace(emoji, f" {letter} ")
    found = set(VOTE.findall(lowered))
    return found.pop().upper() if len(found) == 1 else None


def tally(comments):
    """comments: [(kişi, metin)] -> {"A": n, "B": n}. Aynı kişinin sonraki yorumu öncekinin yerine geçer."""
    latest = {}
    for author, text in comments:
        vote = parse_vote(text)
        if vote:
            latest[author] = vote
    return {"A": sum(v == "A" for v in latest.values()), "B": sum(v == "B" for v in latest.values())}


def youtube_comments(url):
    """Videonun üst düzey yorumları; yanıtlar oy sayılmaz."""
    from googleapiclient.discovery import build

    from ..uploaders.youtube import YouTubeUploader

    match = YOUTUBE_ID.search(url or "")
    if not match:
        return []
    service = build("youtube", "v3", credentials=YouTubeUploader(shorts=True)._credentials(), cache_discovery=False)
    comments, token = [], None
    for _ in range(MAX_PAGES):
        response = service.commentThreads().list(
            part="snippet", videoId=match[1], maxResults=100, textFormat="plainText", pageToken=token,
        ).execute()
        for item in response.get("items", []):
            snippet = item["snippet"]["topLevelComment"]["snippet"]
            author = snippet.get("authorChannelId", {}).get("value") or snippet.get("authorDisplayName", "")
            comments.append((f"yt:{author}", snippet.get("textDisplay", "")))
        token = response.get("nextPageToken")
        if not token:
            break
    return comments


def instagram_comments(media_id):
    from ..uploaders import instagram
    from ..uploaders.base import read_json

    if not media_id or not instagram.CONFIG_FILE.exists():
        return []
    settings = read_json(instagram.CONFIG_FILE)
    url = f"{instagram.GRAPH}/{settings.get('api_version', instagram.DEFAULT_VERSION)}/{media_id}/comments"
    params = {"fields": "text,username", "limit": 50, "access_token": settings["access_token"]}
    comments = []
    for _ in range(MAX_PAGES):
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        comments += [(f"ig:{c.get('username', c.get('id'))}", c.get("text", "")) for c in data.get("data", [])]
        url = data.get("paging", {}).get("next")
        params = None  # sonraki sayfa adresi bütün parametreleri içerir
        if not url:
            break
    return comments
