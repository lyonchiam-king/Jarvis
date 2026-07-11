from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
MENTORS_DIR = DATA_DIR / "mentors"
DIGESTS_DIR = DATA_DIR / "digests"
RAW_TRANSCRIPTS_DIR = DATA_DIR / "raw_transcripts"

load_dotenv(REPO_ROOT / ".env")


def anthropic_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return key


def mentor_dir(slug: str) -> Path:
    return MENTORS_DIR / slug


def mentor_videos_dir(slug: str) -> Path:
    return mentor_dir(slug) / "videos"


def mentor_profile_path(slug: str) -> Path:
    return mentor_dir(slug) / "profile.yaml"


def mentor_knowledge_base_path(slug: str) -> Path:
    return mentor_dir(slug) / "knowledge_base.md"
