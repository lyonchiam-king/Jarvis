"""Rolls up every video's extracted key points into one persona knowledge
base per mentor. This file is what the chat and digest modules ground
themselves in - it's the actual "virtual version" of the person.
"""
from __future__ import annotations

import json

from . import config
from .ingest.extract import load_all_key_points
from .llm import LLMClient

SYNTHESIS_SYSTEM_PROMPT = """You are building a structured knowledge-base profile \
of a real public figure, based only on key points extracted from their own \
published videos. The goal is a document someone can use as grounding context to \
have a mentoring conversation "with" this person. Stay strictly faithful to the \
source material - do not add opinions, facts, or advice that isn't supported by it. \
Write in clear Markdown."""

SYNTHESIS_PROMPT_TEMPLATE = """Mentor: {name}
Focus areas: {focus_areas}

Below is a JSON array of key points extracted from {video_count} of their videos. \
Each entry has: summary, principles, frameworks, actionable_advice, quotes.

{key_points_json}

Synthesize this into a single Markdown profile with these sections:
## Core Identity
A short paragraph on who they are and what they're known for, based only on the material.
## Recurring Principles
The beliefs/principles that show up across multiple videos (dedupe near-duplicates).
## Signature Frameworks
Named or describable frameworks/models they teach, with a one-line explanation each.
## Actionable Playbook
The most concrete, repeatable pieces of advice, as a bullet list.
## Voice & Style
2-4 sentences describing how they talk: tone, directness, favorite phrases - so a \
chat persona can sound like them, not just repeat their advice.
## Notable Quotes
5-10 of the strongest verbatim quotes from the source material.

Return only the Markdown document."""


def synthesize_profile(slug: str, name: str, focus_areas: list[str], llm: LLMClient) -> str:
    key_points = load_all_key_points(slug)
    if not key_points:
        raise ValueError(
            f"No extracted key points for '{slug}' yet. Run `jarvis ingest {slug}` first."
        )

    prompt = SYNTHESIS_PROMPT_TEMPLATE.format(
        name=name,
        focus_areas=", ".join(focus_areas) or "(none specified)",
        video_count=len(key_points),
        key_points_json=json.dumps(key_points, ensure_ascii=False),
    )
    markdown = llm.complete(system=SYNTHESIS_SYSTEM_PROMPT, prompt=prompt, max_tokens=4000)

    path = config.mentor_knowledge_base_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown)
    return markdown
