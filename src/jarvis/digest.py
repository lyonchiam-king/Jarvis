"""Generates a daily nudge from one or more mentors' knowledge bases.

Delivery is pluggable: today this ships a stdout notifier and a file
notifier (append to data/digests/YYYY-MM-DD.md). Wire in Telegram/email by
implementing the Notifier protocol and passing it to `generate_digest`.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Protocol

from . import config
from .llm import LLMClient
from .mentors import Mentor

DIGEST_SYSTEM_PROMPT = """You write a short daily nudge from a virtual mentor \
persona to the person studying them. Ground it strictly in the mentor's knowledge \
base below - pick one principle, framework, or piece of advice and phrase it as a \
direct, personal, actionable nudge for today. 3-5 sentences, first person, in the \
mentor's voice per their "Voice & Style" notes. No preamble, just the nudge."""

DIGEST_PROMPT_TEMPLATE = """# Knowledge base: {name}

{knowledge_base}

Write today's nudge."""


class Notifier(Protocol):
    def send(self, mentor_name: str, text: str) -> None:
        ...


class StdoutNotifier:
    def send(self, mentor_name: str, text: str) -> None:
        print(f"--- {mentor_name} ---\n{text}\n")


class FileNotifier:
    def __init__(self, date: dt.date | None = None):
        self._date = date or dt.date.today()

    def send(self, mentor_name: str, text: str) -> None:
        config.DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.DIGESTS_DIR / f"{self._date.isoformat()}.md"
        existing = path.read_text() if path.exists() else f"# Daily digest - {self._date.isoformat()}\n"
        with path.open("w") as f:
            f.write(existing + f"\n## {mentor_name}\n\n{text}\n")


def generate_digest(mentor: Mentor, llm: LLMClient, notifiers: list[Notifier]) -> str:
    knowledge_base = mentor.knowledge_base()
    if not knowledge_base:
        raise ValueError(
            f"'{mentor.slug}' has no synthesized knowledge base yet. "
            f"Run `jarvis synthesize {mentor.slug}` first."
        )
    prompt = DIGEST_PROMPT_TEMPLATE.format(name=mentor.name, knowledge_base=knowledge_base)
    text = llm.complete(system=DIGEST_SYSTEM_PROMPT, prompt=prompt, max_tokens=400)
    for notifier in notifiers:
        notifier.send(mentor.name, text)
    return text
