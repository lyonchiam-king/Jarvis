"""Pulls video lists and caption transcripts from YouTube.

Deliberately transcript-only: no video/audio download. yt-dlp is used just
to enumerate a channel's uploads (metadata only), and youtube-transcript-api
pulls the existing caption track. This keeps ingestion fast, cheap, and
within normal personal-research use of publicly available captions.
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import config


@dataclass
class VideoMeta:
    video_id: str
    title: str
    url: str


def list_channel_videos(channel_url: str, limit: int | None = None) -> list[VideoMeta]:
    """Enumerate a channel's uploads without downloading anything."""
    import yt_dlp

    videos_url = channel_url.rstrip("/")
    if not videos_url.endswith("/videos"):
        videos_url += "/videos"

    opts = {
        "extract_flat": "in_playlist",
        "quiet": True,
        "no_warnings": True,
        "playlistend": limit,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(videos_url, download=False)

    entries = info.get("entries") or []
    videos = [
        VideoMeta(
            video_id=entry["id"],
            title=entry.get("title") or entry["id"],
            url=f"https://www.youtube.com/watch?v={entry['id']}",
        )
        for entry in entries
        if entry
    ]
    return videos[:limit] if limit else videos


def fetch_transcript(video_id: str, languages: tuple[str, ...] = ("en",)) -> str:
    """Return the plain-text transcript for a video, using cache if present."""
    cache_path = config.RAW_TRANSCRIPTS_DIR / f"{video_id}.txt"
    if cache_path.exists():
        return cache_path.read_text()

    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    fetched = api.fetch(video_id, languages=list(languages))
    text = " ".join(snippet.text for snippet in fetched).strip()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text)
    return text
