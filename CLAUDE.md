# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Jarvis** is a Personal Assistant (PA) that supports pluggable skills. Skills are modular capabilities that Jarvis loads at startup.

## Architecture

```
jarvis.py          # Main PA entry point
skills/
  __init__.py      # Skill registry - auto-loads all available skills
  trader.py        # Trader skill (wraps warren-buffet/trader_skill)
```

## Installed Skills

| Skill  | Source             | Description                               |
|--------|--------------------|-------------------------------------------|
| trader | warren-buffet repo | Warren Buffett-style stock value analysis |

## Setup

```bash
# Install the trader skill from the sibling repo
pip install -e ../warren-buffet

# Run Jarvis
python jarvis.py
```

## Adding a New Skill

1. Create `skills/<name>.py` with a class that has `NAME`, `DESCRIPTION`, and `handle(command, **kwargs) -> str`
2. Register it in `skills/__init__.py` under `AVAILABLE_SKILLS`

## Build & Run

```bash
python jarvis.py
```
