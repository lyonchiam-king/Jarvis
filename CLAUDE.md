# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with this repository.

## Project Overview

**Jarvis** is a Personal Assistant (PA) with pluggable skills. It currently ships two trading capabilities:

| Capability | File | Style |
|---|---|---|
| Daily value-investing analysis | `run_trader_bot.py` | Warren Buffett fundamentals |
| Intraday ICT smart money bot | `run_tjr_bot.py` | TJR AMD model (London/NY sessions) |

## Repository layout

```
jarvis.py              # Jarvis PA entry point (skill system demo)
run_trader_bot.py      # Daily watchlist bot (Warren Buffett style)
run_tjr_bot.py         # TJR intraday bot (ICT AMD model)
watchlist.json         # Tickers for daily analysis
tjr_config.json        # TJR bot config (instrument, account size, broker)
skills/
  __init__.py          # Skill registry
  trader.py            # Trader skill adapter (wraps warren-buffet package)
```

## Setup

```bash
# US stocks (Alpaca)
pip install -e '../warren-buffet[alpaca]'

# Crypto (Binance, Bybit, OKX)
pip install -e '../warren-buffet[crypto]'
```

## Running the bots

### Daily value-investing watchlist
```bash
# Edit watchlist.json, then:
python run_trader_bot.py
```

### TJR intraday bot
```bash
# Set secrets:
export ALPACA_API_KEY=your_key
export ALPACA_SECRET_KEY=your_secret
export TELEGRAM_BOT_TOKEN=your_token   # optional, for alerts
export TELEGRAM_CHAT_ID=your_chat_id   # optional

# Edit tjr_config.json to set instrument and account balance, then:
python run_tjr_bot.py
```

The TJR bot runs continuously. It only trades during London (03:00–08:00 EST) and NY (08:00–11:00 EST) sessions. All trades are logged to `tjr_journal.json`.

## TJR Bot hard limits (never touch)

| Rule | Value |
|---|---|
| Max risk/trade | 2% |
| Max trades/session | 3 |
| Daily loss stop | 3% |
| Post-loss cooldown | 30 min |
| Min R:R | 2:1 |

## Adding a new Jarvis skill

1. Create `skills/<name>.py` with `NAME`, `DESCRIPTION`, and `handle(command, **kwargs) -> str`
2. Register it in `skills/__init__.py` under `AVAILABLE_SKILLS`
