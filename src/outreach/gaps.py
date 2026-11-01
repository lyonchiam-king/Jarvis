"""Which gaps a business may be shown, and the evidence for each one.

The infographic track puts three claims about a real business onto a graphic
with Lyon's name at the bottom. The owner can check every one of them in
seconds, so a wrong claim does not just fail to land -- it is the reason they
stop reading, and it is worse than sending nothing.

A model asked to "name three gaps" will always return three, because that is
what the format asks for. It fills the shape. That is the same failure as
asking it to list the services on offer: it rewords one, drops one, and
invents a fourth that nobody sells.

So the gaps are chosen here, in Python, from the research row -- each one a
rule with a test that either passes or does not. The wording ships with the
rule. The model's only job downstream is to say the same thing in this
business's own vocabulary ("term fees" rather than "prices"), and it may
never add a card.

Three cards is the format, not a quota. Two evidenced gaps means two cards.
Fewer than ``MIN_SPECIFIC`` means this business gets the generic graphic --
no name, no checkable claims -- which is the honest version of not knowing.

Every gap carries an ``evidence`` line. It is never shown to the business:
it is written to the Sheet so that months later any claim on any graphic can
be traced back to the row that justified it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Below this many evidenced gaps, a business gets the generic graphic rather
# than one with its name on it. Two is deliberate: a single card is not a
# "3 quick wins" graphic, and padding it to three is exactly the invention
# this module exists to prevent.
MIN_SPECIFIC = 2

# The format's card count. A lead with more evidenced gaps than this keeps the
# highest-priority ones; it never gets a fourth card.
MAX_CARDS = 3


@dataclass(frozen=True)
class Gap:
    """One problem/solution pair, and why this business qualifies for it."""

    id: str
    # The coral card: what the visitor could not do. Never what the owner did
    # wrong -- someone paid for what is there, quite possibly them, and an
    # insult is a reason to stop reading.
    problem: str
    # The mint card: what the businesses doing well do instead.
    fix: str
    # Why this lead qualifies. Written to the Sheet, never onto the graphic.
    evidence: str
    # Gaps sharing a group are alternatives -- at most one of them is ever
    # chosen, because they are two readings of the same column and showing
    # both would say the same thing twice.
    group: str = ""


def _int(value: object) -> int:
    try:
        return int(str(value or "").strip() or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value or "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _text(row: dict[str, str] | None, key: str) -> str:
    if not row:
        return ""
    return str(row.get(key, "") or "").strip()


# Values research writes into "Customer Pain Points" when the reviews showed
# nothing to fix. Matched case-insensitively. Without this a graphic would
# carry the literal words "none evident" as a problem card.
_NO_PAIN = frozenset(
    {
        "",
        "-",
        "n/a",
        "na",
        "none",
        "none evident",
        "none found",
        "nothing",
        "nothing recorded",
        "no pain points",
        "unknown",
    }
)


def _no_website(lead: dict[str, str], research: dict[str, str] | None) -> Gap | None:
    if _text(lead, "Website Status") != "none":
        return None
    return Gap(
        id="no_website",
        problem="Nothing to send when someone asks",
        fix="One page that answers the usual questions",
        evidence="Google lists no website for this business",
        group="web_presence",
    )


def _social_only(lead: dict[str, str], research: dict[str, str] | None) -> Gap | None:
    if _text(lead, "Website Status") != "social_only":
        return None
    return Gap(
        id="social_only",
        problem="Everything lives in the feed",
        fix="A page that does not scroll away",
        evidence=(
            "Their only web presence is a social page: "
            f"{_text(lead, 'Website URL') or 'no URL recorded'}"
        ),
        group="web_presence",
    )


def _review_pain(lead: dict[str, str], research: dict[str, str] | None) -> Gap | None:
    """The one gap whose wording genuinely comes from this business.

    Their own customers wrote it, which makes it the strongest evidence on
    the row -- and the most dangerous card on the graphic. The problem text
    here is deliberately about what the customer could not do, never about
    what the business did badly, and the localisation prompt downstream
    repeats that rule. A graphic telling an owner what their customers
    complain about is an insult with his name under it.
    """
    pain = _text(research, "Customer Pain Points")
    if pain.casefold() in _NO_PAIN:
        return None
    return Gap(
        id="review_pain",
        problem="The same question keeps coming up",
        fix="Answer it before they have to ask",
        # Quoted so the card can be traced back to the words it came from.
        evidence=f"From their own reviews: {pain}",
        group="reviews_content",
    )


def _strong_but_invisible(
    lead: dict[str, str], research: dict[str, str] | None
) -> Gap | None:
    rating = _float(lead.get("Rating"))
    count = _int(lead.get("Review Count"))
    if rating < 4.5 or count < 10:
        return None
    if _text(lead, "Website Status") == "real":
        return None
    return Gap(
        id="strong_but_invisible",
        problem="Great reviews, nowhere to send them",
        fix="Put the proof where people look first",
        evidence=f"{rating:g} stars from {count} reviews, with no website carrying them",
        group="review_volume",
    )


def _few_reviews(lead: dict[str, str], research: dict[str, str] | None) -> Gap | None:
    count = _int(lead.get("Review Count"))
    if not 1 <= count < 10:
        return None
    return Gap(
        id="few_reviews",
        problem=f"Only {count} review{'' if count == 1 else 's'} to go on",
        fix="Ask every happy customer, once",
        evidence=f"Google shows {count} review(s)",
        group="review_volume",
    )


def _no_email(lead: dict[str, str], research: dict[str, str] | None) -> Gap | None:
    """Only when research actually looked and found nothing.

    ``Email Source`` is blank on a row written before email discovery ran, or
    with it switched off. A blank cell is missing evidence, not evidence of
    absence -- claiming "no way to reach you but phone" about a business whose
    address was simply never searched for is exactly the kind of checkable
    error this module exists to stop.
    """
    if _text(research, "Email Found"):
        return None
    if _text(research, "Email Source").casefold() != "not found":
        return None
    return Gap(
        id="no_email",
        problem="The phone is the only way in",
        fix="An enquiry form that reaches the inbox",
        evidence="Research searched the site and socials and found no email address",
    )


# Ordered by how much the evidence is worth, strongest first. A lead with more
# evidenced gaps than MAX_CARDS keeps the ones nearest the top.
#
# review_pain leads because their own customers wrote it. The web-presence
# gaps come next: they are the campaign's whole argument and they are checkable
# in one glance. The review-volume readings and the missing email are true and
# useful but weaker openings.
RULES = (
    _review_pain,
    _no_website,
    _social_only,
    _strong_but_invisible,
    _no_email,
    _few_reviews,
)


# The generic graphic, for a business the research cannot say three true things
# about. No name on it and no checkable claims -- these are gaps every small
# business recognises, which is what makes it safe to send unevidenced.
GENERIC_GAPS = (
    Gap(
        id="generic_manual",
        problem="Time going on jobs a tool could do",
        fix="Automate the repeated ones",
        evidence="Generic graphic -- not a claim about this business",
    ),
    Gap(
        id="generic_dropoff",
        problem="Enquiries that go quiet",
        fix="One quick follow-up, every time",
        evidence="Generic graphic -- not a claim about this business",
    ),
    Gap(
        id="generic_message",
        problem="Too many things on offer at once",
        fix="Lead with the one that sells",
        evidence="Generic graphic -- not a claim about this business",
    ),
)


def gaps_for(
    lead: dict[str, str],
    research: dict[str, str] | None,
    *,
    max_cards: int = MAX_CARDS,
    use_review_pain: bool = True,
) -> tuple[Gap, ...]:
    """The evidenced gaps for this lead, strongest first.

    Returns fewer than ``max_cards`` when the evidence supports fewer, and an
    empty tuple when it supports none. The caller decides what too few means
    -- see ``is_specific_enough``.

    ``use_review_pain`` switches off the one card whose wording comes from
    customer complaints. It is the strongest evidence and the easiest to
    render as an insult, so it is a campaign's decision rather than a
    constant.
    """
    chosen: list[Gap] = []
    groups_used: set[str] = set()

    for rule in RULES:
        if len(chosen) >= max_cards:
            break
        if rule is _review_pain and not use_review_pain:
            continue
        gap = rule(lead, research)
        if gap is None:
            continue
        if gap.group and gap.group in groups_used:
            continue
        if gap.group:
            groups_used.add(gap.group)
        chosen.append(gap)

    return tuple(chosen)


def is_specific_enough(found: tuple[Gap, ...]) -> bool:
    """Whether these gaps earn a graphic with the business's name on it."""
    return len(found) >= MIN_SPECIFIC


def with_wording(gap: Gap, problem: str, fix: str) -> Gap:
    """A gap re-worded for this business, keeping its id and its evidence.

    The id and the evidence are the audit trail and are never the model's to
    change; only the two lines a reader sees may be localised. Blank or
    whitespace-only replacements leave the library wording in place, so a
    model that returns nothing useful degrades to the text that shipped
    rather than to an empty card.
    """
    return replace(
        gap,
        problem=problem.strip() or gap.problem,
        fix=fix.strip() or gap.fix,
    )


def gaps_summary(found: tuple[Gap, ...]) -> str:
    """The ``Gaps Used`` cell: one line per card, id and its evidence.

    This is the whole point of the column. A claim on a graphic sent months
    ago is defensible only if the row still says what justified it.
    """
    return "\n".join(f"{g.id}: {g.evidence}" for g in found)
