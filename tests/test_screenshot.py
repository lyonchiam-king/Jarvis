"""Photographing the built site, and never letting that cost a message.

The picture is the nice-to-have; the message is the deliverable. Every
failure here -- no browser, no page, a browser that hangs or writes nothing
-- has to end with the message still going out.
"""

import subprocess
from pathlib import Path

import pytest

from outreach import screenshot


def _page(tmp_path) -> Path:
    page = tmp_path / "sites" / "salon-a1b2" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text("<!doctype html><title>Salon</title><h1>Salon</h1>")
    return page


class TestFindingABrowser:
    def test_an_explicit_path_is_used_when_it_exists(self, tmp_path):
        exe = tmp_path / "msedge.exe"
        exe.write_text("")
        assert screenshot.find_browser(str(exe)) == str(exe)

    def test_an_explicit_path_that_does_not_exist_is_refused(self, tmp_path):
        """Falling back silently would photograph with something the user
        did not choose, or hang. Better to report no browser."""
        assert screenshot.find_browser(str(tmp_path / "nope.exe")) == ""


class TestCapture:
    def test_a_missing_page_is_reported_not_photographed(self, tmp_path):
        with pytest.raises(screenshot.ScreenshotError, match="No page"):
            screenshot.capture(tmp_path / "gone.html", tmp_path / "out.png")

    def test_no_browser_says_so_and_names_the_fix(self, tmp_path):
        page = _page(tmp_path)
        with pytest.raises(screenshot.ScreenshotError, match="screenshot_browser"):
            screenshot.capture(
                page, tmp_path / "out.png", browser=str(tmp_path / "nope.exe")
            )

    def test_the_browser_is_driven_headless_at_the_asked_size(self, tmp_path):
        page = _page(tmp_path)
        out = tmp_path / "out.png"
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            out.write_bytes(b"\x89PNG")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        exe = tmp_path / "msedge"
        exe.write_text("")
        shot = screenshot.capture(
            page, out, browser=str(exe), width=800, height=1000, runner=fake_run
        )

        assert shot.path == out
        assert "--headless=new" in seen["cmd"]
        assert "--window-size=800,1000" in seen["cmd"]
        assert any(a.startswith("--screenshot=") for a in seen["cmd"])
        assert seen["cmd"][-1].startswith("file://"), "the local page is the source"

    def test_a_private_profile_is_used(self, tmp_path):
        """Without one, a browser the user already has open refuses to start
        a second instance and returns success having written nothing."""
        page = _page(tmp_path)
        out = tmp_path / "out.png"
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            out.write_bytes(b"\x89PNG")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        exe = tmp_path / "msedge"
        exe.write_text("")
        screenshot.capture(page, out, browser=str(exe), runner=fake_run)

        assert any(a.startswith("--user-data-dir=") for a in seen["cmd"])

    def test_a_zero_exit_that_wrote_nothing_is_still_a_failure(self, tmp_path):
        """Headless Chrome reports success and writes no file often enough
        that the exit code cannot be trusted. Bytes on disk are the proof."""
        page = _page(tmp_path)
        exe = tmp_path / "msedge"
        exe.write_text("")

        def wrote_nothing(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, b"", b"blank page")

        with pytest.raises(screenshot.ScreenshotError, match="wrote no image"):
            screenshot.capture(
                page, tmp_path / "out.png", browser=str(exe), runner=wrote_nothing
            )

    def test_a_hung_browser_gives_up_rather_than_blocking_the_run(self, tmp_path):
        page = _page(tmp_path)
        exe = tmp_path / "msedge"
        exe.write_text("")

        def hangs(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 60)

        with pytest.raises(screenshot.ScreenshotError, match="did not finish"):
            screenshot.capture(
                page, tmp_path / "out.png", browser=str(exe), runner=hangs
            )


class TestWhereTheShotLives:
    def test_it_sits_beside_the_page(self, tmp_path):
        page = _page(tmp_path)
        assert screenshot.shot_path_for(page) == page.parent / "screenshot.png"

    def test_no_schema_column_is_needed_to_find_it_again(self, tmp_path):
        """The directory is already derived from the lead's slug, so the
        picture is always findable without storing a path that could go
        stale against a Sheet."""
        page = _page(tmp_path)
        assert screenshot.shot_path_for(page).parent == page.parent


class TestAPictureIsNeverWorthTheMessage:
    """The rule that governs this whole feature.

    The screenshot is decoration on top of the thing being sent. Every way
    it can fail has to end with the message still going.
    """

    def test_a_draft_survives_a_browser_that_is_not_there(self, tmp_path, monkeypatch):
        from outreach.config import load_config
        from outreach.stages import outreach_draft

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        monkeypatch.chdir(tmp_path)

        # No site, no browser, nothing at all -- and no exception.
        assert outreach_draft.capture_site_shot(cfg, {"Business Name": "X"}, "a") is None

    def test_a_screenshot_error_is_swallowed_not_raised(self, tmp_path, monkeypatch):
        from outreach.config import load_config
        from outreach import screenshot as shot_mod
        from outreach.stages import outreach_draft

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        monkeypatch.chdir(tmp_path)

        page = tmp_path / cfg.build.output_dir / "x" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<h1>x</h1>")

        monkeypatch.setattr(
            outreach_draft, "site_page_for", lambda *a, **k: page
        )

        def explode(*a, **k):
            raise shot_mod.ScreenshotError("the browser fell over")

        monkeypatch.setattr(shot_mod, "capture", explode)

        assert outreach_draft.capture_site_shot(cfg, {"Business Name": "X"}, "a") is None

    def test_an_existing_shot_is_not_taken_again(self, tmp_path, monkeypatch):
        """Re-photographing an unchanged page costs seconds per lead and
        buys nothing."""
        from outreach.config import load_config
        from outreach import screenshot as shot_mod
        from outreach.stages import outreach_draft

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        monkeypatch.chdir(tmp_path)

        page = tmp_path / "sites" / "x" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text("<h1>x</h1>")
        (page.parent / "screenshot.png").write_bytes(b"\x89PNG already")

        monkeypatch.setattr(outreach_draft, "site_page_for", lambda *a, **k: page)
        monkeypatch.setattr(
            shot_mod, "capture", lambda *a, **k: pytest.fail("photographed twice")
        )

        got = outreach_draft.capture_site_shot(cfg, {"Business Name": "X"}, "a")
        assert got == page.parent / "screenshot.png"

    def test_an_unreadable_image_still_sends_the_text(self):
        """The client must drop a bad attachment, not the message."""
        import json as _j

        import responses

        from outreach.providers.whatsapp import WhatsAppClient

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                "http://127.0.0.1:4099/send",
                json={"status": "sent", "message_id": "m1", "timestamp": "t",
                      "dialled": "60123456789", "ack": 1},
            )
            result = WhatsAppClient().send_message(
                "60123456789", "Hi", "", image_path="/no/such/file.png"
            )
            body = _j.loads(rsps.calls[0].request.body)

        assert result.status == "sent"
        assert "image" not in body, "a missing file must not become an empty attachment"
        assert body["message"] == "Hi"


class TestASiteThatOnlyExistsAtAUrl:
    """Why the first version photographed nothing for Lyon.

    It looked only for a local sites/<slug>/index.html, which exists for
    pages this pipeline generates. Lyon builds in Google AI Studio, so his
    sites exist only at a Preview URL -- there was nothing on disk to
    photograph and the failure was silent.
    """

    def test_a_url_is_something_a_browser_can_open(self):
        assert screenshot.source_uri("https://acus-salon.web.app") == (
            "https://acus-salon.web.app"
        )

    def test_a_local_page_still_becomes_a_file_uri(self, tmp_path):
        page = _page(tmp_path)
        assert screenshot.source_uri(page).startswith("file://")

    def test_neither_gives_nothing_rather_than_a_broken_command(self, tmp_path):
        assert screenshot.source_uri(tmp_path / "gone.html") == ""
        assert screenshot.source_uri("") == ""

    def test_a_url_is_photographed_with_time_to_load(self, tmp_path):
        """A remote page is blank for a moment; a local file is not."""
        out = tmp_path / "out.png"
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            out.write_bytes(b"\x89PNG")
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, b"", b"")

        exe = tmp_path / "msedge"
        exe.write_text("")
        screenshot.capture(
            "https://acus-salon.web.app", out, browser=str(exe), runner=fake_run
        )

        assert seen["cmd"][-1] == "https://acus-salon.web.app"
        assert any(a.startswith("--virtual-time-budget=") for a in seen["cmd"])

    def test_the_draft_uses_the_preview_url_when_nothing_is_on_disk(
        self, tmp_path, monkeypatch
    ):
        from outreach.config import load_config
        from outreach import screenshot as shot_mod
        from outreach.stages import outreach_draft

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        monkeypatch.chdir(tmp_path)
        asked = {}

        def fake_capture(source, out, **kw):
            asked["source"] = source
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"\x89PNG")
            return shot_mod.Shot(path=Path(out), width=900, height=1200)

        monkeypatch.setattr(shot_mod, "capture", fake_capture)

        got = outreach_draft.capture_site_shot(
            cfg, {"Business Name": "Acus Salon"}, "a",
            "https://acus-salon.web.app",
        )

        assert asked["source"] == "https://acus-salon.web.app"
        assert got is not None and got.exists()

    def test_no_page_and_no_url_is_simply_no_picture(self, tmp_path, monkeypatch):
        from outreach.config import load_config
        from outreach.stages import outreach_draft

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        monkeypatch.chdir(tmp_path)

        assert outreach_draft.capture_site_shot(
            cfg, {"Business Name": "X"}, "a", ""
        ) is None


class TestNoScreenshotFailureCanKillADraft:
    """The regression that produced nothing at all.

    capture_site_shot caught only ScreenshotError. Anything else -- a
    permissions error writing the file, an OSError from the browser process,
    anything Windows-specific -- escaped into the per-lead error guard and
    killed that lead's draft. When it happened for every lead the run
    produced no messages whatsoever.
    """

    @pytest.mark.parametrize(
        "boom",
        [
            OSError("permission denied"),
            RuntimeError("something odd"),
            ValueError("bad path"),
            MemoryError(),
        ],
    )
    def test_any_exception_is_swallowed(self, tmp_path, monkeypatch, boom):
        from outreach.config import load_config
        from outreach import screenshot as shot_mod
        from outreach.stages import outreach_draft

        cfg = load_config("config/campaigns/penang-my.yaml", env_file=None)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            outreach_draft, "site_page_for", lambda *a, **k: None
        )

        def explode(*a, **k):
            raise boom

        monkeypatch.setattr(shot_mod, "capture", explode)

        assert (
            outreach_draft.capture_site_shot(
                cfg, {"Business Name": "X"}, "a", "https://example.test"
            )
            is None
        )

    def test_a_whole_draft_survives_a_broken_screenshot(self, tmp_path, monkeypatch):
        """The end-to-end version: the message is still written."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from test_outreach_draft import (  # noqa: E402
            FakeGmail,
            FakeSheets,
            FakeWriter,
            cfg,
            index,
            lead,
            research_row,
        )

        from outreach.stages import outreach_draft

        def explode(*a, **k):
            raise OSError("the browser exploded")

        monkeypatch.setattr(outreach_draft.screenshot, "capture", explode)
        monkeypatch.setattr(
            outreach_draft, "site_page_for", lambda *a, **k: None
        )

        sheets = FakeSheets(index([lead("a")]), index([research_row("a")]))
        report = outreach_draft.run(cfg(), sheets, FakeWriter(), FakeGmail())

        assert report.errors == 0, "a failed picture must not be logged as a lead error"
        row = sheets.upserts[0][1][0]
        assert row["WhatsApp Message"].startswith("Hi!"), "the message still exists"


class TestHeadlessModesAreTriedUntilOneWorks:
    """Lyon's Edge accepted --headless=new, exited 0, and wrote nothing.

    Which headless mode honours --screenshot depends on the browser build
    on the machine, and there is no way to know from here which one that is.
    So the modes are tried in turn, and only a browser that fails all of
    them is a failure.
    """

    def _exe(self, tmp_path):
        exe = tmp_path / "msedge"
        exe.write_text("")
        return exe

    def _runner(self, tmp_path, works_in, out):
        """A browser that only writes an image in the named mode."""
        calls = []

        def run(cmd, **kw):
            import subprocess as sp

            mode = next(a for a in cmd if a.startswith("--headless"))
            calls.append(mode)
            if mode == works_in:
                out.write_bytes(b"\x89PNG")
            return sp.CompletedProcess(cmd, 0, b"", b"noise")

        return run, calls

    def test_the_first_mode_is_used_when_it_works(self, tmp_path):
        out = tmp_path / "out.png"
        run, calls = self._runner(tmp_path, "--headless=new", out)
        screenshot.capture(
            _page(tmp_path), out, browser=str(self._exe(tmp_path)), runner=run
        )
        assert calls == ["--headless=new"], "no second launch when the first works"

    def test_it_falls_back_to_old_headless(self, tmp_path):
        """The case that was failing: new writes nothing, old works."""
        out = tmp_path / "out.png"
        run, calls = self._runner(tmp_path, "--headless=old", out)
        shot = screenshot.capture(
            _page(tmp_path), out, browser=str(self._exe(tmp_path)), runner=run
        )
        assert shot.path == out
        assert calls == ["--headless=new", "--headless=old"]

    def test_a_stray_file_in_the_working_directory_is_claimed(
        self, tmp_path, monkeypatch
    ):
        """Some builds ignore --screenshot's path and write to the cwd."""
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "shots" / "out.png"
        out.parent.mkdir()

        def run(cmd, **kw):
            import subprocess as sp

            (tmp_path / "screenshot.png").write_bytes(b"\x89PNG")
            return sp.CompletedProcess(cmd, 0, b"", b"")

        shot = screenshot.capture(
            _page(tmp_path), out, browser=str(self._exe(tmp_path)), runner=run
        )
        assert shot.path == out and out.read_bytes() == b"\x89PNG"

    def test_failing_every_mode_names_what_was_tried(self, tmp_path):
        def run(cmd, **kw):
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, b"", b"Something real went wrong")

        with pytest.raises(screenshot.ScreenshotError) as err:
            screenshot.capture(
                _page(tmp_path), tmp_path / "out.png",
                browser=str(self._exe(tmp_path)), runner=run,
            )

        message = str(err.value)
        assert "--headless=new" in message and "--headless=old" in message
        assert "Something real went wrong" in message

    def test_chrome_noise_is_not_reported_as_the_cause(self, tmp_path):
        """"Every renderer should have at least one task provided by a
        primary provider" is a warning every headless run emits. Putting it
        forward as the reason sends someone hunting the wrong thing."""
        noise = (
            b"[ERROR:fallback_task_provider.cc:126] Every renderer should "
            b"have at least one task provided by a primary provider"
        )

        def run(cmd, **kw):
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, b"", noise)

        with pytest.raises(screenshot.ScreenshotError) as err:
            screenshot.capture(
                _page(tmp_path), tmp_path / "out.png",
                browser=str(self._exe(tmp_path)), runner=run,
            )

        assert "Every renderer" not in str(err.value)
        assert "nothing explaining why" in str(err.value)
