"""Trader skill adapter for Jarvis - wraps warren-buffet's TraderSkill."""

import sys
import os

_WARREN_BUFFET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "warren-buffet")
if os.path.isdir(_WARREN_BUFFET_PATH):
    sys.path.insert(0, os.path.abspath(_WARREN_BUFFET_PATH))

try:
    from trader_skill import TraderSkill as _TraderSkill
    _SKILL_AVAILABLE = True
except ImportError:
    _SKILL_AVAILABLE = False


class TraderSkillAdapter:
    """Jarvis skill wrapper around the Warren-Buffet TraderSkill."""

    NAME = "trader"
    DESCRIPTION = "Analyze stocks with Warren Buffett-style value investing"

    def __init__(self):
        if not _SKILL_AVAILABLE:
            raise RuntimeError(
                "TraderSkill not found. Install warren-buffet package: "
                "pip install -e ../warren-buffet"
            )
        self._skill = _TraderSkill()

    def handle(self, command: str, **kwargs) -> str:
        """
        Handle Jarvis commands for the trader skill.

        Supported commands:
          analyze    - analyze a stock (kwargs: ticker, metrics dict)
          screen     - screen multiple stocks (kwargs: candidates list)
          philosophy - explain value investing philosophy
        """
        if command == "analyze":
            ticker = kwargs.get("ticker", "")
            metrics = kwargs.get("metrics", {})
            rec = self._skill.analyze(ticker, metrics)
            return (
                f"[{rec.action}] {rec.ticker} (confidence: {rec.confidence:.0%})\n"
                f"{rec.reasoning}"
            )
        elif command == "screen":
            candidates = kwargs.get("candidates", [])
            recs = self._skill.screen_stocks(candidates)
            lines = [
                f"[{r.action}] {r.ticker} ({r.confidence:.0%}) — {r.reasoning}"
                for r in recs
            ]
            return "\n".join(lines)
        elif command == "philosophy":
            return self._skill.explain_philosophy()
        else:
            return f"Unknown trader command: '{command}'. Try: analyze, screen, philosophy"
