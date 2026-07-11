import pytest

from jarvis import config


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own data/ directory instead of the real repo data."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "MENTORS_DIR", tmp_path / "mentors")
    monkeypatch.setattr(config, "DIGESTS_DIR", tmp_path / "digests")
    monkeypatch.setattr(config, "RAW_TRANSCRIPTS_DIR", tmp_path / "raw_transcripts")
    return tmp_path
