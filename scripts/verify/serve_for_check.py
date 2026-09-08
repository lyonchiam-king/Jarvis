"""Serve the dashboard on a fixed port so a real browser can load it.

Verification only. The suite uses Flask's test client, which never runs the
page's JavaScript -- and a page that parses is not a page that renders. This
puts the app on a socket so Chromium can fetch it, execute the bundle and
show what a person would actually see.

No password is set, so the app binds loopback only and asks for no login.
Nothing here touches the Sheet: without credentials every data endpoint
answers with an error the page is expected to survive, which is itself worth
proving.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from outreach.config import load_config  # noqa: E402
from outreach.dashboard import create_app  # noqa: E402


def main() -> None:
    port = int(sys.argv[1])
    config = load_config("config/campaigns/penang-my.yaml", env_file=None)
    app = create_app(config)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
