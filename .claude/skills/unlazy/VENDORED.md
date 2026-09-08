# Vendored, not a submodule

Source:  https://github.com/Leonxlnx/unlazy
Commit:  16671491f6679ad9378f52604d3bc2415b4120c7  (2026-09-03)
Licence: MIT -- see LICENSE, kept intact.

Copied in whole rather than added as a submodule or an npx install, for the
same reason the rest of this project has no build step: it has to work on a
Windows machine after `git pull`, with nothing to install and nothing to
resolve. Node 16+ is the only requirement and it has no runtime dependencies.

`.github/` was left out -- its CI belongs to the upstream repository, not to
this one.

To update, re-copy from a newer commit and record it here. Do not edit the
files in place: a local change would be silently lost by the next update, and
there would be no way to tell what had been changed.
