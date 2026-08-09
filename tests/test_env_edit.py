"""Writing one key into .env without disturbing a working setup."""

import pytest

from outreach.env_edit import EnvEditError, is_set, set_key

SAMPLE = """\
# Google Places -- see the README.
PLACES_API_KEY=abc123
SHEET_ID=1XyZ

# The model.
ANTHROPIC_API_KEY=sk-ant-old
"""


def env(tmp_path, text=SAMPLE):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_an_existing_key_is_replaced_in_place(tmp_path):
    path = env(tmp_path)
    set_key(path, "ANTHROPIC_API_KEY", "sk-ant-new")

    assert "ANTHROPIC_API_KEY=sk-ant-new" in path.read_text()
    assert "sk-ant-old" not in path.read_text()


def test_the_other_keys_and_comments_survive(tmp_path):
    path = env(tmp_path)
    set_key(path, "ANTHROPIC_API_KEY", "new")

    text = path.read_text()
    assert "PLACES_API_KEY=abc123" in text
    assert "SHEET_ID=1XyZ" in text
    assert "# Google Places -- see the README." in text
    assert "# The model." in text


def test_a_new_key_is_appended(tmp_path):
    path = env(tmp_path)
    set_key(path, "OLLAMA_API_KEY", "olm-1")

    assert "OLLAMA_API_KEY=olm-1" in path.read_text()
    assert "PLACES_API_KEY=abc123" in path.read_text()


def test_a_missing_env_file_is_created(tmp_path):
    path = tmp_path / ".env"
    set_key(path, "OPENROUTER_API_KEY", "or-1")
    assert path.read_text() == "OPENROUTER_API_KEY=or-1\n"


def test_a_duplicate_assignment_is_collapsed(tmp_path):
    """dotenv takes the last one, so a stale duplicate below would win."""
    path = env(tmp_path, "OLLAMA_API_KEY=old\nSHEET_ID=x\nOLLAMA_API_KEY=older\n")
    set_key(path, "OLLAMA_API_KEY", "new")

    text = path.read_text()
    assert text.count("OLLAMA_API_KEY") == 1
    assert "OLLAMA_API_KEY=new" in text
    assert "SHEET_ID=x" in text


def test_an_exported_assignment_is_still_matched(tmp_path):
    path = env(tmp_path, "export OLLAMA_API_KEY=old\n")
    set_key(path, "OLLAMA_API_KEY", "new")

    assert path.read_text().strip() == "OLLAMA_API_KEY=new"


def test_a_key_containing_a_hash_is_quoted(tmp_path):
    """Unquoted, dotenv would truncate it at the comment marker."""
    path = env(tmp_path)
    set_key(path, "OLLAMA_API_KEY", "abc#def")

    assert 'OLLAMA_API_KEY="abc#def"' in path.read_text()


def test_a_key_with_a_quote_is_escaped(tmp_path):
    path = env(tmp_path)
    set_key(path, "OLLAMA_API_KEY", 'a"b c')
    assert r'OLLAMA_API_KEY="a\"b c"' in path.read_text()


def test_a_line_break_in_a_key_is_refused(tmp_path):
    with pytest.raises(EnvEditError, match="line break"):
        set_key(env(tmp_path), "OLLAMA_API_KEY", "abc\ndef")


def test_a_nonsense_variable_name_is_refused(tmp_path):
    with pytest.raises(EnvEditError, match="not a valid"):
        set_key(env(tmp_path), "rm -rf /", "x")


def test_the_file_ends_up_owner_only(tmp_path):
    import os
    import stat

    path = env(tmp_path)
    set_key(path, "OLLAMA_API_KEY", "secret")
    mode = stat.S_IMODE(os.stat(path).st_mode)

    assert mode == 0o600, "a file of API keys should not be world-readable"


def test_is_set_reports_presence_without_the_value(tmp_path):
    path = env(tmp_path, "A=filled\nB=\nC=\"\"\n")

    assert is_set(path, "A") is True
    assert is_set(path, "B") is False
    assert is_set(path, "C") is False
    assert is_set(path, "MISSING") is False


def test_is_set_on_a_missing_file_is_false(tmp_path):
    assert is_set(tmp_path / "nope", "A") is False


def test_gemini_api_key_presence_is_reported_without_the_value(tmp_path):
    """Nothing provider-specific in env_edit -- this just pins that GEMINI_API_KEY
    gets the same write-only treatment as every other provider's key."""
    path = env(tmp_path)
    assert is_set(path, "GEMINI_API_KEY") is False

    set_key(path, "GEMINI_API_KEY", "gm-super-secret")

    assert is_set(path, "GEMINI_API_KEY") is True
    assert "gm-super-secret" in path.read_text(), "set_key does write it -- just never echoes it"


def test_a_value_written_is_read_back_by_dotenv(tmp_path):
    """The real contract: python-dotenv must agree with what we wrote."""
    from dotenv import dotenv_values

    path = env(tmp_path)
    set_key(path, "OLLAMA_API_KEY", "has#hash and spaces")

    assert dotenv_values(path)["OLLAMA_API_KEY"] == "has#hash and spaces"
    assert dotenv_values(path)["PLACES_API_KEY"] == "abc123"
