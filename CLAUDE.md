# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Jarvis** is a Personal Assistant (PA) that supports pluggable skills. Skills are modular capabilities that Jarvis loads at startup.

## Architecture

```
jarvis.py              # Main PA entry point
run_trader_bot.py      # Daily trader bot runner (standalone)
watchlist.json         # Daily stock watchlist - edit to add/remove tickers
skills/
  __init__.py          # Skill registry - auto-loads all available skills
  trader.py            # Trader skill (wraps warren-buffet/trader_skill)
```

## Installed Skills

| Skill  | Source             | Description                                    |
|--------|--------------------|------------------------------------------------|
| trader | warren-buffet repo | Warren Buffett-style stock value analysis      |

## Setup

```bash
# Install the trader skill from the sibling repo
pip install -e ../warren-buffet

# Also installs yfinance for live market data
```

## Running the Daily Trader Bot

```bash
# Edit your watchlist
nano watchlist.json

# Run the bot - fetches live data and prints BUY/HOLD/SELL recommendations
python run_trader_bot.py
```

The bot saves a dated report (e.g. `report_2026-05-03.txt`) after each run.

## Using the Trader Skill via Jarvis

```python
from jarvis import Jarvis

jarvis = Jarvis()

# Fetch live data and analyze a single stock
print(jarvis.ask("trader", "fetch", ticker="KO"))

# Run the full daily analysis
print(jarvis.ask("trader", "daily", watchlist=["AAPL", "KO", "JNJ", "MSFT"]))
```

## Adding a New Skill

1. Create `skills/<name>.py` with a class that has `NAME`, `DESCRIPTION`, and `handle(command, **kwargs) -> str`
2. Register it in `skills/__init__.py` under `AVAILABLE_SKILLS`

## Build & Run

```bash
python jarvis.py          # demo run
python run_trader_bot.py  # daily analysis
```
