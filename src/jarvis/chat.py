"""Interactive chat with one mentor, or a mastermind panel of several."""
from __future__ import annotations

from .llm import LLMClient
from .mentors import Mentor
from .persona import build_mastermind_system_prompt, build_system_prompt


class ChatSession:
    """Holds conversation history for one persona (single mentor or panel)."""

    def __init__(self, system_prompt: str, llm: LLMClient):
        self._system_prompt = system_prompt
        self._llm = llm
        self._history: list[dict] = []

    def ask(self, message: str) -> str:
        self._history.append({"role": "user", "content": message})
        reply = self._llm.reply(system=self._system_prompt, history=self._history)
        self._history.append({"role": "assistant", "content": reply})
        return reply


def start_mentor_session(mentor: Mentor, llm: LLMClient) -> ChatSession:
    return ChatSession(build_system_prompt(mentor), llm)


def start_mastermind_session(mentors: list[Mentor], llm: LLMClient) -> ChatSession:
    return ChatSession(build_mastermind_system_prompt(mentors), llm)


def run_repl(session: ChatSession, label: str) -> None:
    print(f"Chatting with {label}. Type 'exit' or Ctrl-D to quit.\n")
    while True:
        try:
            message = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue
        if message.lower() in {"exit", "quit"}:
            break
        reply = session.ask(message)
        print(f"\n{reply}\n")
