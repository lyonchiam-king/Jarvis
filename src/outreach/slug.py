"""Deterministic, unguessable slugs for a lead's demo site URL.

Not `guide.slugify` -- that one is for markdown heading anchors: no hash,
different rules, and a different job entirely. This slug becomes a public
URL, which changes what has to hold:

- **Deterministic.** A re-run of the build stage must produce the same slug
  for the same lead, or resumability breaks -- the stage would think a site
  that already exists is missing and pay to rebuild it.
- **Unguessable.** Two salons named "Sun Salon" in different towns must not
  collide, and a stranger trimming a friend's link back to the business name
  must not land on a *different* business's demo. The Lead ID -- stable,
  unique, never chosen by a human -- is hashed in to guarantee both.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_MAX_NAME_LEN = 40


def site_slug(business_name: str, lead_id: str) -> str:
    """`("Sun Salon", "ChIJxyz")` -> `"sun-salon-7f3a"`.

    The name half is decoration: readable in a URL, disposable if it comes
    out empty. The hash half is load-bearing on every lead, always 4 hex
    characters, always present, and is what actually keeps the URL
    deterministic and hard to guess.
    """
    # NFKD folds an accented character into its base letter plus a separate
    # combining mark (e.g. "é" -> "e" + U+0301); dropping everything outside
    # a-z0-9 after that discards the mark and keeps the letter, which is the
    # fold a human reads as "the accent went away" rather than "the letter
    # went away".
    folded = unicodedata.normalize("NFKD", business_name or "")
    ascii_only = folded.encode("ascii", "ignore").decode("ascii")
    name = _NON_ALNUM.sub("-", ascii_only.lower()).strip("-")
    name = name[:_MAX_NAME_LEN].rstrip("-")

    digest = hashlib.sha256((lead_id or "").encode()).hexdigest()[:4]

    # A name that folds to nothing -- all-CJK, all-emoji, all-punctuation --
    # must not leave a dangling separator in front of the hash. This is the
    # case that crashes a naive `f"{name}-{digest}"`.
    return f"{name}-{digest}" if name else digest
