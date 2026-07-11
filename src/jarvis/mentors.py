from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from . import config


@dataclass
class Mentor:
    slug: str
    name: str
    channel_url: str
    focus_areas: list[str] = field(default_factory=list)
    voice_notes: str = ""

    @classmethod
    def load(cls, slug: str) -> "Mentor":
        path = config.mentor_profile_path(slug)
        if not path.exists():
            raise FileNotFoundError(
                f"No mentor profile at {path}. Run `jarvis mentor add` first."
            )
        raw = yaml.safe_load(path.read_text())
        return cls(
            slug=slug,
            name=raw["name"],
            channel_url=raw["channel_url"],
            focus_areas=raw.get("focus_areas", []),
            voice_notes=raw.get("voice_notes", ""),
        )

    def save(self) -> None:
        path = config.mentor_profile_path(self.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "name": self.name,
            "channel_url": self.channel_url,
            "focus_areas": self.focus_areas,
            "voice_notes": self.voice_notes,
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False))

    def has_knowledge_base(self) -> bool:
        return config.mentor_knowledge_base_path(self.slug).exists()

    def knowledge_base(self) -> str:
        path = config.mentor_knowledge_base_path(self.slug)
        if not path.exists():
            return ""
        return path.read_text()


def list_mentors() -> list[str]:
    if not config.MENTORS_DIR.exists():
        return []
    return sorted(
        p.name for p in config.MENTORS_DIR.iterdir()
        if p.is_dir() and (p / "profile.yaml").exists()
    )
