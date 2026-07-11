import json

from jarvis import config
from jarvis.chat import start_mastermind_session, start_mentor_session
from jarvis.digest import FileNotifier, StdoutNotifier, generate_digest
from jarvis.ingest.extract import (
    _parse_json_object,
    extract_key_points,
    load_all_key_points,
    save_video_key_points,
)
from jarvis.ingest.youtube import VideoMeta
from jarvis.mentors import Mentor, list_mentors
from jarvis.synthesize import synthesize_profile

from .fakes import FakeLLMClient

FAKE_KNOWLEDGE_BASE = "## Core Identity\nTest mentor.\n## Voice & Style\nDirect.\n"


def make_mentor(slug="test-mentor", name="Test Mentor") -> Mentor:
    mentor = Mentor(slug=slug, name=name, channel_url="https://www.youtube.com/@test", focus_areas=["testing"])
    mentor.save()
    return mentor


def test_mentor_save_and_load_roundtrip():
    make_mentor()
    loaded = Mentor.load("test-mentor")
    assert loaded.name == "Test Mentor"
    assert loaded.focus_areas == ["testing"]
    assert list_mentors() == ["test-mentor"]


def test_parse_json_object_handles_fenced_code_block():
    raw = '```json\n{"summary": "hi", "principles": []}\n```'
    assert _parse_json_object(raw) == {"summary": "hi", "principles": []}


def test_extract_key_points_saves_and_loads():
    mentor = make_mentor()
    video = VideoMeta(video_id="abc123", title="Test Video", url="https://youtu.be/abc123")
    llm = FakeLLMClient(complete_response=json.dumps({
        "summary": "A test video.",
        "principles": ["Ship fast"],
        "frameworks": [],
        "actionable_advice": ["Write tests"],
        "quotes": ["Do the work."],
    }))

    key_points = extract_key_points(video, "some transcript text", llm)
    save_video_key_points(mentor.slug, key_points)

    loaded = load_all_key_points(mentor.slug)
    assert len(loaded) == 1
    assert loaded[0]["video_id"] == "abc123"
    assert loaded[0]["principles"] == ["Ship fast"]
    assert config.mentor_videos_dir(mentor.slug).joinpath("abc123.json").exists()


def test_synthesize_profile_requires_ingested_videos():
    mentor = make_mentor()
    llm = FakeLLMClient(complete_response=FAKE_KNOWLEDGE_BASE)
    try:
        synthesize_profile(mentor.slug, mentor.name, mentor.focus_areas, llm)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_synthesize_profile_writes_knowledge_base():
    mentor = make_mentor()
    video = VideoMeta(video_id="abc123", title="Test Video", url="https://youtu.be/abc123")
    save_video_key_points(mentor.slug, {
        "video_id": video.video_id, "title": video.title, "url": video.url,
        "summary": "s", "principles": [], "frameworks": [], "actionable_advice": [], "quotes": [],
    })

    llm = FakeLLMClient(complete_response=FAKE_KNOWLEDGE_BASE)
    markdown = synthesize_profile(mentor.slug, mentor.name, mentor.focus_areas, llm)

    assert markdown == FAKE_KNOWLEDGE_BASE
    assert config.mentor_knowledge_base_path(mentor.slug).read_text() == FAKE_KNOWLEDGE_BASE
    assert Mentor.load(mentor.slug).has_knowledge_base()


def test_chat_session_single_mentor_grounds_in_knowledge_base():
    mentor = make_mentor()
    config.mentor_knowledge_base_path(mentor.slug).write_text(FAKE_KNOWLEDGE_BASE)

    llm = FakeLLMClient(reply_response="Here's my advice.")
    session = start_mentor_session(mentor, llm)
    reply = session.ask("What should I focus on?")

    assert reply == "Here's my advice."
    assert FAKE_KNOWLEDGE_BASE in llm.reply_calls[0]["system"]
    assert llm.reply_calls[0]["history"] == [
        {"role": "user", "content": "What should I focus on?"},
    ]
    assert session._history == [
        {"role": "user", "content": "What should I focus on?"},
        {"role": "assistant", "content": "Here's my advice."},
    ]


def test_mastermind_session_includes_all_mentors():
    mentor_a = make_mentor("mentor-a", "Mentor A")
    mentor_b = make_mentor("mentor-b", "Mentor B")
    config.mentor_knowledge_base_path("mentor-a").write_text("# A knowledge\n")
    config.mentor_knowledge_base_path("mentor-b").write_text("# B knowledge\n")

    llm = FakeLLMClient(reply_response="Panel says hi.")
    session = start_mastermind_session([Mentor.load("mentor-a"), Mentor.load("mentor-b")], llm)
    session.ask("Any advice?")

    system_prompt = llm.reply_calls[0]["system"]
    assert "Mentor A" in system_prompt and "# A knowledge" in system_prompt
    assert "Mentor B" in system_prompt and "# B knowledge" in system_prompt


def test_generate_digest_writes_file_and_prints(capsys):
    mentor = make_mentor()
    config.mentor_knowledge_base_path(mentor.slug).write_text(FAKE_KNOWLEDGE_BASE)

    llm = FakeLLMClient(complete_response="Today's nudge: ship the thing.")
    text = generate_digest(mentor, llm, [StdoutNotifier(), FileNotifier()])

    assert text == "Today's nudge: ship the thing."
    assert "Today's nudge" in capsys.readouterr().out

    digest_files = list(config.DIGESTS_DIR.glob("*.md"))
    assert len(digest_files) == 1
    assert "Today's nudge" in digest_files[0].read_text()
    assert mentor.name in digest_files[0].read_text()
