"""Turns one video transcript into structured key points via the LLM."""
from __future__ import annotations

import json

from .. import config
from ..llm import LLMClient
from .youtube import VideoMeta

EXTRACTION_SYSTEM_PROMPT = """You are a research assistant that distills long-form \
video transcripts into a structured set of key points for someone building a \
personal knowledge base about the speaker. Be faithful to what the speaker actually \
said - do not invent advice they didn't give. Output strict JSON only, no prose \
outside the JSON object."""

EXTRACTION_PROMPT_TEMPLATE = """Video title: {title}

Transcript:
{transcript}

Extract the following as a JSON object with exactly these keys:
- "summary": one or two sentence summary of what the video is about
- "principles": list of general principles or beliefs the speaker expresses
- "frameworks": list of named or describable step-by-step frameworks/models taught
- "actionable_advice": list of concrete, actionable recommendations
- "quotes": list of short, notable verbatim quotes (max 5)

Return only the JSON object."""


def build_extraction_prompt(video: VideoMeta, transcript: str) -> str:
    return EXTRACTION_PROMPT_TEMPLATE.format(title=video.title, transcript=transcript)


def extract_key_points(video: VideoMeta, transcript: str, llm: LLMClient) -> dict:
    prompt = build_extraction_prompt(video, transcript)
    raw = llm.complete(system=EXTRACTION_SYSTEM_PROMPT, prompt=prompt, max_tokens=1500)
    data = _parse_json_object(raw)
    data["video_id"] = video.video_id
    data["title"] = video.title
    data["url"] = video.url
    return data


def _parse_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"LLM response did not contain a JSON object: {raw!r}")
    return json.loads(text[start : end + 1])


def save_video_key_points(slug: str, key_points: dict) -> None:
    videos_dir = config.mentor_videos_dir(slug)
    videos_dir.mkdir(parents=True, exist_ok=True)
    path = videos_dir / f"{key_points['video_id']}.json"
    path.write_text(json.dumps(key_points, indent=2, ensure_ascii=False))


def load_all_key_points(slug: str) -> list[dict]:
    videos_dir = config.mentor_videos_dir(slug)
    if not videos_dir.exists():
        return []
    results = []
    for path in sorted(videos_dir.glob("*.json")):
        results.append(json.loads(path.read_text()))
    return results
