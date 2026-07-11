from __future__ import annotations

import click

from . import config
from .chat import run_repl, start_mastermind_session, start_mentor_session
from .digest import FileNotifier, StdoutNotifier, generate_digest
from .ingest.extract import extract_key_points, save_video_key_points
from .ingest.youtube import fetch_transcript, list_channel_videos
from .llm import AnthropicClient
from .mentors import Mentor, list_mentors
from .synthesize import synthesize_profile


def _llm() -> AnthropicClient:
    return AnthropicClient(config.anthropic_api_key())


@click.group()
def main() -> None:
    """Jarvis: a personal consultant made of virtual mentors."""


@main.group()
def mentor() -> None:
    """Manage the mentor roster."""


@mentor.command("add")
@click.option("--slug", required=True, help="Short id, e.g. iman-gadzhi")
@click.option("--name", required=True, help="Display name")
@click.option("--channel-url", required=True, help="YouTube channel URL")
@click.option("--focus", multiple=True, help="Focus area, repeatable")
@click.option("--voice-notes", default="", help="Free-text notes on how they talk")
def mentor_add(slug: str, name: str, channel_url: str, focus: tuple[str], voice_notes: str) -> None:
    m = Mentor(slug=slug, name=name, channel_url=channel_url, focus_areas=list(focus), voice_notes=voice_notes)
    m.save()
    click.echo(f"Added mentor '{slug}' -> {config.mentor_profile_path(slug)}")


@mentor.command("list")
def mentor_list() -> None:
    slugs = list_mentors()
    if not slugs:
        click.echo("No mentors yet. Add one with `jarvis mentor add`.")
        return
    for slug in slugs:
        m = Mentor.load(slug)
        status = "synthesized" if m.has_knowledge_base() else "not synthesized"
        click.echo(f"{slug}: {m.name} ({status})")


@main.command()
@click.argument("slug")
@click.option("--limit", default=None, type=int, help="Max videos to ingest")
def ingest(slug: str, limit: int | None) -> None:
    """Fetch a mentor's channel videos, transcribe, and extract key points."""
    m = Mentor.load(slug)
    llm = _llm()
    videos = list_channel_videos(m.channel_url, limit=limit)
    click.echo(f"Found {len(videos)} videos for {m.name}.")
    for video in videos:
        video_path = config.mentor_videos_dir(slug) / f"{video.video_id}.json"
        if video_path.exists():
            click.echo(f"  skip (already ingested): {video.title}")
            continue
        try:
            transcript = fetch_transcript(video.video_id)
        except Exception as exc:  # transcript may be disabled/unavailable
            click.echo(f"  skip (no transcript): {video.title} ({exc})")
            continue
        key_points = extract_key_points(video, transcript, llm)
        save_video_key_points(slug, key_points)
        click.echo(f"  extracted: {video.title}")


@main.command()
@click.argument("slug")
def synthesize(slug: str) -> None:
    """Roll up a mentor's extracted videos into one knowledge base."""
    m = Mentor.load(slug)
    llm = _llm()
    synthesize_profile(slug, m.name, m.focus_areas, llm)
    click.echo(f"Wrote {config.mentor_knowledge_base_path(slug)}")


@main.command()
@click.argument("slugs", nargs=-1, required=True)
def chat(slugs: tuple[str]) -> None:
    """Chat with one mentor, or pass several slugs for a mastermind panel."""
    llm = _llm()
    mentors = [Mentor.load(slug) for slug in slugs]
    if len(mentors) == 1:
        session = start_mentor_session(mentors[0], llm)
        run_repl(session, mentors[0].name)
    else:
        session = start_mastermind_session(mentors, llm)
        run_repl(session, " + ".join(m.name for m in mentors))


@main.command()
@click.argument("slugs", nargs=-1)
@click.option("--to", "destinations", multiple=True, default=("stdout", "file"),
              type=click.Choice(["stdout", "file"]), help="Where to send the digest")
def digest(slugs: tuple[str], destinations: tuple[str]) -> None:
    """Generate today's nudge from one or more mentors (default: all)."""
    llm = _llm()
    target_slugs = list(slugs) or list_mentors()
    if not target_slugs:
        click.echo("No mentors yet. Add one with `jarvis mentor add`.")
        return
    notifiers = []
    if "stdout" in destinations:
        notifiers.append(StdoutNotifier())
    if "file" in destinations:
        notifiers.append(FileNotifier())
    for slug in target_slugs:
        m = Mentor.load(slug)
        generate_digest(m, llm, notifiers)


if __name__ == "__main__":
    main()
