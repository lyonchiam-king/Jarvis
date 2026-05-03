"""Trader skill adapter for Jarvis - wraps warren-buffet's TraderSkill."""

import sys
import os

_WARREN_BUFFET_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "warren-buffet")
if os.path.isdir(_WARREN_BUFFET_PATH):
    sys.path.insert(0, os.path.abspath(_WARREN_BUFFET_PATH))

try:
    from trader_skill import TraderSkill as _TraderSkill, DailyTraderBot as _DailyTraderBot
    from trader_skill.data import fetch_metrics as _fetch_metrics
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
        Supported commands:
          analyze    - analyze a single stock (kwargs: ticker, metrics dict)
          fetch      - fetch live data + analyze (kwargs: ticker)
          screen     - screen multiple stocks (kwargs: candidates list)
          daily      - run full daily bot (kwargs: watchlist list of tickers)
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
        elif command == "fetch":
            ticker = kwargs.get("ticker", "")
            metrics = _fetch_metrics(ticker)
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
        elif command == "daily":
            watchlist = kwargs.get("watchlist", [])
            bot = _DailyTraderBot(watchlist)
            results = bot.run(verbose=False)
            return bot.report(results)
        elif command == "philosophy":
            return self._skill.explain_philosophy()
        else:
            return (
                f"Unknown trader command: '{command}'. "
                "Try: analyze, fetch, screen, daily, philosophy"
            )
