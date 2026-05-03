"""Jarvis daily trader bot - analyze watchlist and print trading recommendations."""

import json
import os
import sys
from datetime import date

_WARREN_BUFFET_PATH = os.path.join(os.path.dirname(__file__), "..", "warren-buffet")
if os.path.isdir(_WARREN_BUFFET_PATH):
    sys.path.insert(0, os.path.abspath(_WARREN_BUFFET_PATH))

try:
    from trader_skill import DailyTraderBot
except ImportError:
    print("Error: trader-skill not found. Run: pip install -e ../warren-buffet")
    sys.exit(1)

WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "watchlist.json")


def load_watchlist() -> list:
    with open(WATCHLIST_PATH) as f:
        data = json.load(f)
    return data.get("tickers", [])


if __name__ == "__main__":
    print("Jarvis Trader Bot")
    print("-" * 30)

    tickers = load_watchlist()
    if not tickers:
        print("No tickers in watchlist.json. Add some tickers and retry.")
        sys.exit(0)

    print(f"Analyzing {len(tickers)} stocks from watchlist...\n")

    bot = DailyTraderBot(tickers)
    results = bot.run(verbose=True)

    print()
    report = bot.report(results)
    print(report)

    report_file = f"report_{date.today()}.txt"
    with open(report_file, "w") as f:
        f.write(report)
    print(f"\nReport saved to {report_file}")
