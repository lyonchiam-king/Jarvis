"""
Jarvis TJR Trader Bot entry point.

Setup
-----
1. Install dependencies:
       pip install -e '../warren-buffet[alpaca]'   # US stocks
       pip install -e '../warren-buffet[crypto]'   # crypto

2. Set secrets as environment variables (never hard-code them):
       export ALPACA_API_KEY=your_key
       export ALPACA_SECRET_KEY=your_secret
       export TELEGRAM_BOT_TOKEN=your_token   # optional
       export TELEGRAM_CHAT_ID=your_chat_id   # optional

3. Edit tjr_config.json to set instrument, account size, etc.

4. Run:
       python run_tjr_bot.py
"""

import os
import sys
import json
import logging

_WARREN_BUFFET_PATH = os.path.join(os.path.dirname(__file__), "..", "warren-buffet")
if os.path.isdir(_WARREN_BUFFET_PATH):
    sys.path.insert(0, os.path.abspath(_WARREN_BUFFET_PATH))

try:
    from tjr import TJRBot
    from tjr.brokers import AlpacaBroker, CCXTBroker
except ImportError as e:
    print(f"Import error: {e}")
    print("Install: pip install -e '../warren-buffet[alpaca]'")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S UTC",
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "tjr_config.json")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def build_broker(cfg: dict):
    mode = cfg.get("broker", "alpaca").lower()
    paper = cfg.get("paper_trading", True)

    if mode == "alpaca":
        return AlpacaBroker(
            api_key=os.environ.get("ALPACA_API_KEY") or cfg.get("alpaca_api_key", ""),
            secret_key=os.environ.get("ALPACA_SECRET_KEY") or cfg.get("alpaca_secret_key", ""),
            paper=paper,
        )
    elif mode == "ccxt":
        return CCXTBroker(
            exchange_id=cfg.get("ccxt_exchange", "binance"),
            api_key=os.environ.get("CCXT_API_KEY") or cfg.get("ccxt_api_key", ""),
            secret=os.environ.get("CCXT_SECRET") or cfg.get("ccxt_secret", ""),
            paper=paper,
        )
    else:
        raise ValueError(f"Unknown broker: {mode}. Choose 'alpaca' or 'ccxt'.")


if __name__ == "__main__":
    cfg    = load_config()
    broker = build_broker(cfg)

    bot = TJRBot(
        instrument=cfg["instrument"],
        account_balance=cfg["account_balance"],
        broker=broker,
        risk_pct=cfg.get("risk_pct", 0.01),
    )

    bot.run(poll_secs=cfg.get("poll_interval_secs", 30))
