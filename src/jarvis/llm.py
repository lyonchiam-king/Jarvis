"""Thin wrapper around the Claude API so every other module depends on
this narrow interface instead of the SDK directly. That's what makes the
rest of the pipeline (extraction, synthesis, chat, digest) testable with a
fake client and no network access or API key.
"""
from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str:
        """Single-turn completion, used by extraction/synthesis/digest."""

    def reply(self, *, system: str, history: list[dict], max_tokens: int = 1500) -> str:
        """Multi-turn completion, used by the chat REPL and mastermind panel."""


class AnthropicClient:
    """Real client, backed by the Anthropic SDK. Constructed lazily so
    importing this module never requires the SDK or an API key.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-5"):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str:
        return self.reply(
            system=system,
            history=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )

    def reply(self, *, system: str, history: list[dict], max_tokens: int = 1500) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=history,
        )
        return "".join(
            block.text for block in response.content if block.type == "text"
        )
