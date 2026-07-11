"""A fake LLM client so the pipeline can be tested end-to-end with no
network access and no API key.
"""
from __future__ import annotations


class FakeLLMClient:
    def __init__(self, complete_response: str = "", reply_response: str = ""):
        self.complete_response = complete_response
        self.reply_response = reply_response
        self.complete_calls: list[dict] = []
        self.reply_calls: list[dict] = []

    def complete(self, *, system: str, prompt: str, max_tokens: int = 2000) -> str:
        self.complete_calls.append({"system": system, "prompt": prompt})
        return self.complete_response

    def reply(self, *, system: str, history: list[dict], max_tokens: int = 1500) -> str:
        self.reply_calls.append({"system": system, "history": list(history)})
        return self.reply_response
