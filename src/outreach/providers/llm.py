"""Turn raw signals about a business into a structured research brief.

Uses Claude Opus 5 with structured outputs, so the response is guaranteed to
match the schema rather than being parsed out of prose.

Compliance note: review text is passed to the model here and the *summary* is
what gets written to the Sheet. Raw review text is never persisted, which keeps
the pipeline inside Google's caching terms.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("outreach")

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tone": {
            "type": "string",
            "description": "How this business presents itself, in 3-8 words.",
        },
        "services": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Specific services or products, from the evidence only.",
        },
        "price_tier": {
            "type": "string",
            "enum": ["budget", "mid-range", "premium", "unknown"],
        },
        "customer_praise": {
            "type": "string",
            "description": "What customers consistently compliment. One or two sentences.",
        },
        "customer_pain_points": {
            "type": "string",
            "description": (
                "Recurring complaints or friction, especially anything a website "
                "would fix. 'None evident' if the reviews are uniformly positive."
            ),
        },
        "suggested_website_angle": {
            "type": "string",
            "description": (
                "One concrete pitch angle for this specific business, naming the "
                "problem a site would solve. Two sentences at most."
            ),
        },
    },
    "required": [
        "tone",
        "services",
        "price_tier",
        "customer_praise",
        "customer_pain_points",
        "suggested_website_angle",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You brief a founder who sells simple, well-built websites to small businesses \
that don't have one yet. He reads these briefs before writing a first outreach \
message, so they need to be specific enough to quote back to the owner.

Ground every field in the evidence you are given. If the evidence doesn't \
support a claim, say so plainly rather than inventing something plausible -- a \
brief that says "not evident from reviews" is useful, a fabricated detail is \
worse than useless because he may repeat it to the owner.

For the website angle: name the specific friction a site would remove for this \
business. "They get booking questions in the reviews and have no way to take \
bookings online" is useful. "A website would improve their online presence" is \
not.

Write for a reader, not a template. No marketing language, no filler.
"""


SITE_SPEC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "visitor": {
            "type": "string",
            "description": (
                "Who lands on this site, on what device, in what state of mind, "
                "and the one thing they need within ten seconds. Be specific to "
                "this business -- 'someone whose usual barber is closed, on a "
                "phone, at 8pm' beats 'potential customers'."
            ),
        },
        "site_goal": {
            "type": "string",
            "description": (
                "The one job this site must do. One sentence, one job -- if you "
                "name two, you have not decided yet."
            ),
        },
        "pages": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Page names. Keep it small -- 1 to 4 for an SME. A single "
                "excellent page beats four thin ones; only add a page when the "
                "content genuinely cannot live on the home page."
            ),
        },
        "hero_headline": {
            "type": "string",
            "description": (
                "Real copy, not a placeholder. Under 60 characters. It must be "
                "impossible to paste onto a competitor's site -- if it would "
                "work for any salon in town, it is not the headline."
            ),
        },
        "hero_subcopy": {
            "type": "string",
            "description": (
                "One sentence under the headline that answers 'and why should I "
                "care?'. Concrete, no adjective stacking."
            ),
        },
        "key_sections": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Home page sections in scroll order, each as 'Name -- UI "
                "pattern -- what it contains'. Name the pattern so a builder "
                "knows the shape: sticky nav, hero with overlay, badge strip, "
                "card grid opening a modal, tabbed builder, two-column story, "
                "review ticker, map and hours. Order by what the visitor "
                "needs, not by convention."
            ),
        },
        "primary_cta": {
            "type": "string",
            "description": (
                "The exact button text for the single action the whole site "
                "drives at. Verb first, 2-4 words, in the visitor's language "
                "not the business's."
            ),
        },
        "proof_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Trust signals that are TRUE from the evidence given -- a "
                "rating with its review count, a named specialty customers "
                "repeatedly praise, years in business if stated. Empty array if "
                "the evidence supports none. Never manufacture one."
            ),
        },
        "palette": {
            "type": "string",
            "description": (
                "Actual decisions with hex values and where each is used: page "
                "background, body text, one accent for the CTA. Justify the "
                "choice from the business, not from taste. Three or four "
                "colours total -- more is a sign of an undecided design."
            ),
        },
        "typography": {
            "type": "string",
            "description": (
                "Named typefaces available on Google Fonts, one for headings "
                "and one for body, with the size and weight relationship "
                "between them. Say why this pairing suits this business."
            ),
        },
        "avoid": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Specific things that would be wrong for THIS business -- the "
                "cliches of its category, the tone that would ring false. "
                "'No stock photos of models' is useful; 'nothing ugly' is not."
            ),
        },
        "visual_direction": {
            "type": "string",
            "description": (
                "The feel in two or three sentences, tying the palette and type "
                "together. What should someone sense in the first second?"
            ),
        },
        "highlights": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Exactly three short badges for a strip under the hero -- the "
                "differentiators a customer would care about, 2-5 words each. "
                "True from the evidence only. Think 'Pork-Free', 'Open Kitchen', "
                "'Walk-ins Welcome', 'Closed Thursdays'."
            ),
        },
        "showcase_items": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "3-6 of the things this business actually sells, each as "
                "'Name -- tag, tag'. The tags are sensory or benefit badges, "
                "2-4 words. Dishes for a restaurant, treatments for a salon, "
                "subjects for a tuition centre. Named specifically enough that "
                "a card could be built for each -- 'Haircut' is not an item, "
                "'Scalp-first cut and treatment' is. Empty only if the evidence "
                "names nothing at all."
            ),
        },
        "interactive_feature": {
            "type": "string",
            "description": (
                "One bespoke interactive tool that mirrors how this business "
                "actually sells, described well enough to build: the control, "
                "the steps, and what updates live. A set-menu builder with "
                "tabs and a running total; a 'find your treatment' picker with "
                "three questions; a slot chooser that composes a WhatsApp "
                "message. This is the thing that makes the site feel built for "
                "them rather than generated. It must work by touch and must "
                "end at the primary action."
            ),
        },
        "mobile_bar": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The one or two actions pinned to the bottom of the screen on "
                "mobile, as 'Label -> what it does'. Usually calling and "
                "messaging. This is where most conversions happen on a phone."
            ),
        },
        "signature_moment": {
            "type": "string",
            "description": (
                "The one interaction someone would describe to a friend. It "
                "must belong to THIS business -- a barber's before/after that "
                "wipes as you drag, a cafe's menu that fills as you scroll. "
                "One idea, achievable in CSS or Framer Motion, and it must not "
                "sit between the visitor and the primary action."
            ),
        },
        "motion_language": {
            "type": "string",
            "description": (
                "How this site moves, specifically: entrance behaviour, "
                "duration in ms, easing curve, stagger between siblings. "
                "Motion should match the business -- a spa eases slowly, a "
                "barber snaps. 'Smooth animations' is not an answer."
            ),
        },
        "micro_interactions": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Named feedback moments, each as 'trigger -> response'. Cover "
                "at minimum the primary CTA, the form, and one delight. Every "
                "one must work by touch -- hover does not exist on a phone."
            ),
        },
    },
    "required": [
        "visitor",
        "site_goal",
        "pages",
        "hero_headline",
        "hero_subcopy",
        "key_sections",
        "primary_cta",
        "proof_points",
        "palette",
        "typography",
        "avoid",
        "visual_direction",
        "signature_moment",
        "motion_language",
        "micro_interactions",
        "highlights",
        "showcase_items",
        "interactive_feature",
        "mobile_bar",
    ],
    "additionalProperties": False,
}

SITE_SPEC_SYSTEM = """\
You are a senior web designer. You have spent years building small sites for \
local businesses, and you are writing the brief that a builder will implement \
literally. Write it the way the best designers do.

**Start from the visitor, not the business.** Nobody visits a salon website to \
admire it. They arrive on a phone, one-handed, mid-errand, needing one thing: \
are you open, where are you, can I book. Name that person and that moment \
first, and every later decision follows from it. A brief that opens with "a \
modern, professional presence" is a brief written by someone who skipped this \
step.

**Decide one job, then defend it.** Every section must move the visitor toward \
the primary action. If a section doesn't, cut it -- and say so in `avoid`. The \
discipline of a good small site is in what it leaves out. Most SME sites fail \
by being a brochure when they should be a booking button.

**Write real copy.** The headline you produce ships. It must be specific enough \
that pasting it onto a competitor's site would be obviously wrong. "Quality \
haircuts in Georgetown" is not a headline; it is a category. Use the words \
customers themselves used in the reviews -- they are better copywriters than \
either of us, and their phrasing is proof you listened.

**Make real design decisions.** Name hex values, name typefaces, name the \
relationship between heading and body size. "Modern and clean" is not a \
direction, it is an evasion -- it tells the builder nothing and it will produce \
the same beige template every other business has. Derive the choices from the \
business: a 30-year-old barber shop and a new lash studio should not resolve to \
the same palette, and if yours do, you have designed neither.

**Design for the phone first and the thumb specifically.** Assume a mid-range \
Android on a patchy connection. Tap targets big, phone number tappable, no \
hover-dependent anything, nothing important below three scrolls.

**Make it move like someone decided how it moves.** The difference between a \
template and a site that feels expensive is almost entirely motion and \
feedback: things enter with intent, buttons answer the finger, the page has \
rhythm. So specify motion the way a designer briefs it -- duration in \
milliseconds, an easing curve, what staggers against what -- not as "smooth \
animations", which tells the builder nothing and produces the default fade \
every template already has.

Match the motion to the business. A spa eases in slowly and settles; a barber \
snaps; a tuition centre is crisp and unfussy. If your motion brief would suit \
any of the three equally, you have not designed it.

**Name the pattern, and fill it with real things.** A builder can execute \
"card grid, four cards, each opening a modal" and cannot execute "a section \
about our services". So say which UI pattern each section is, and then \
enumerate what goes in it -- the actual named treatments, dishes or courses, \
each with two short tags. A brief that says "showcase the menu" produces a \
placeholder; one that lists four dishes with their tags produces a page.

**Give it one tool that mirrors how they sell.** Not decoration -- a small \
interactive thing that does a job: a set-menu builder with tabs and a running \
total, a three-question picker that lands on the right treatment, a slot \
chooser that composes the WhatsApp message for them. This is what separates a \
site built for this business from a template with their name on it. It must \
work by touch, and it must end at the primary action.

**Give it one signature moment.** One interaction someone would mention to a \
friend, and it must be about this business -- a before/after that wipes as you \
drag your thumb, a price list that assembles as it enters, a gallery that \
settles like laid photographs. One. A site with five signature moments has \
none, and every one of them delays the booking.

**Then defend the visitor from all of it.** This is where award-site thinking \
goes wrong for a local business. The techniques that win galleries -- WebGL \
scenes, scroll-jacking, cursor effects, loading screens, animation that must \
finish before content is readable -- exist for portfolios whose job is to be \
impressive. This site's job is to get a booking from someone on 4G who is \
mildly annoyed already. Motion here earns its place by making the page feel \
considered and responsive, never by making anyone wait. Nothing you specify \
may delay the primary action, hide content until an animation completes, or \
depend on a mouse.

**Never invent a fact.** No prices you weren't given, no awards, no staff names, \
no testimonials, no "established 1998" unless the evidence says so. A fabricated \
detail is worse than a missing one: the owner will see this site, and one wrong \
claim tells them you never looked at their business. Where a real detail is \
needed but absent, the builder will mark it [TO CONFIRM] -- that is the correct \
outcome, not a failure.

**Their social feed and their Google Business photos are the image library \
this site will be built from.** Write `visual_direction` and `avoid` against \
what is actually photographable for THIS business -- the real corner shoplot, \
the real chairs, the real dishes on the pass -- not a mood that would need \
photography nobody has taken.

**Raise the ambition of `signature_moment` and `motion_language`.** The \
audience for this brief has already seen the default template fade; give the \
builder something with more intent than that, sized to what this business \
actually is. The one constraint that does not move: the hero itself does not \
animate. It is visible and complete the instant the page paints, whatever \
else on the page moves.

Every field you fill is an instruction someone will follow exactly. Write \
nothing you would not defend in a review.
"""

OUTREACH_COPY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {
            "type": "string",
            "description": "Email subject. Specific to this business, under 60 characters.",
        },
        "email_body": {
            "type": "string",
            "description": (
                "The email itself, plain text, no signature block -- the "
                "signature and compliance footer are appended separately. "
                "Under 150 words."
            ),
        },
        "whatsapp_message": {
            "type": "string",
            "description": (
                "A much shorter WhatsApp opener for the same business. Under 60 "
                "words, conversational, no subject line, no formal signoff."
            ),
        },
    },
    "required": ["subject", "email_body", "whatsapp_message"],
    "additionalProperties": False,
}

OUTREACH_COPY_SYSTEM = """\
You write first-contact messages from a founder to a small business owner who \
has never heard of him. Professional but warm -- the register of a competent \
neighbour, not a marketing department.

The whole message earns its keep in the first sentence, and that sentence must \
prove you actually looked at their business. Reference something specific and \
true from the research brief. Generic openers ("I hope this email finds you \
well", "I came across your business") get deleted unread.

Rules:
- Say the specific thing you noticed, then the specific thing you'd do about it.
- One ask, and make it small -- a reply, not a meeting.
- No hype, no superlatives, no "revolutionise", no exclamation marks.
- Never claim you are an existing customer, never invent a mutual connection, \
never imply a prior relationship. Everything you say must be true from the \
brief alone.
- The WhatsApp version is not the email pasted shorter -- it is a message a \
person would actually send on their phone.
"""


# Models that reject `output_config.effort` with a 400. Effort arrived with the
# 4.6 generation; the older small models predate it. Sending it to one of these
# fails every single lead, so the parameter is omitted rather than gambled.
_MODELS_WITHOUT_EFFORT = (
    "claude-haiku-4-5",
    "claude-haiku-3",
    "claude-sonnet-4-5",
    "claude-sonnet-3",
    "claude-opus-4-1",
    "claude-opus-4-0",
)


def supports_effort(model: str) -> bool:
    return not any(model.startswith(prefix) for prefix in _MODELS_WITHOUT_EFFORT)


class LLMError(Exception):
    """The model could not produce a brief for this lead."""


@dataclass(frozen=True)
class ResearchBrief:
    tone: str
    services: tuple[str, ...]
    price_tier: str
    customer_praise: str
    customer_pain_points: str
    suggested_website_angle: str

    @property
    def services_text(self) -> str:
        return ", ".join(self.services)


@dataclass(frozen=True)
class SiteSpec:
    site_goal: str
    pages: tuple[str, ...]
    hero_headline: str
    hero_subcopy: str
    key_sections: tuple[str, ...]
    primary_cta: str
    visual_direction: str
    # The design decisions. Defaulted so a spec built from the older seven
    # fields still constructs; the model is required to fill all of them.
    visitor: str = ""
    proof_points: tuple[str, ...] = ()
    highlights: tuple[str, ...] = ()
    showcase_items: tuple[str, ...] = ()
    interactive_feature: str = ""
    mobile_bar: tuple[str, ...] = ()
    palette: str = ""
    typography: str = ""
    avoid: tuple[str, ...] = ()
    signature_moment: str = ""
    motion_language: str = ""
    micro_interactions: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutreachCopy:
    subject: str
    email_body: str
    whatsapp_message: str


def render_lovable_prompt(
    *,
    business_name: str,
    category: str,
    location: str,
    spec: SiteSpec,
    phone: str = "",
    address: str = "",
    social_handle: str = "",
    social_profiles: str = "",
    maps_url: str = "",
) -> str:
    """Turn the structured spec into the text you paste into Lovable.

    Deterministic on purpose: the model decides *what* the site should be, this
    function decides how that is worded. Same spec in, same prompt out, which
    makes it reviewable, diffable, and testable -- and means a bad site is
    traceable to either the spec or this template, never to a coin flip.

    Structured the way a designer hands work over: who it's for, the one job,
    the copy, the art direction, what to leave out, then the build rules.
    """
    where = f" in {location}" if location else ""
    pages = "\n".join(f"- {p}" for p in spec.pages) or "- Home (single page)"
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(spec.key_sections, start=1))

    out = [
        f"Build a website for {business_name}, a "
        f"{category or 'local business'}{where}.",
        "",
        "This business has never had a website. This site is the first "
        "impression anyone gets of them online, and the owner will see it "
        "before their customers do.",
    ]

    sources = []
    if maps_url:
        sources.append(
            f"- Google Business Profile: {maps_url}\n"
            "  Read the recent reviews and look at the customer photos. Take "
            "the words customers actually use for this place and put them in "
            "the copy -- they write better than either of us. Match the real "
            "interior: if the photos show a bright corner shoplot, do not "
            "design a dark luxury salon."
        )
    for line in (social_profiles or "").splitlines():
        if line.strip():
            sources.append(f"- {line.strip()}")
    if sources:
        out += [
            "",
            "STUDY THESE FIRST, BEFORE YOU DESIGN ANYTHING",
            *sources,
            "",
            "These are the business's own public pages. Use them for their real "
            "colours, their real photographs, their actual tone of voice, and "
            "what they genuinely offer. If you cannot open one, say so plainly "
            "and work from the brief below -- do not invent what you would have "
            "found there.",
            "",
            "IMAGERY",
            "Pull every photo you can from the sources above before you design "
            "anything: Google Business Profile photos (the owner's own, and the "
            "ones customers attached to their reviews -- this one almost always "
            "opens), then Instagram, then Facebook. Instagram and Facebook are "
            "login-walled and often will not let a fetch through; if one will "
            "not open, say which one plainly and work from whichever sources "
            "did -- never invent a photo you could not see.",
            "",
            "Then select. Sharp, well-lit, and it shows the actual place or the "
            "actual work -- use it as-is; a real customer photo of this Penang "
            "shoplot outranks any stock image, however polished. The actual "
            "place or work but blurry or a dated phone snapshot -- recreate the "
            "same subject and framing to a higher standard; a dim 2019 "
            "snapshot does not outrank a stock image, the shot it should have "
            "been does. Never substitute stock photography for either, and "
            "never fill a gap with an invented photo -- an honest tinted "
            "placeholder beats a fabricated one. The hero image must come from "
            "the real premises or the real work; nothing generic sits above "
            "the fold.",
        ]

    if spec.visitor:
        out += ["", "WHO IT IS FOR", spec.visitor]

    out += [
        "",
        "THE ONE JOB",
        spec.site_goal,
        "",
        f'Every element serves this. If a section does not move someone toward '
        f'"{spec.primary_cta}", it does not belong on the page.',
        "",
        "PAGES",
        pages,
        "",
        "HERO (use this copy as written)",
        f"Headline: {spec.hero_headline}",
        f"Subcopy:  {spec.hero_subcopy}",
        f"Button:   {spec.primary_cta}",
        "",
        "HOME PAGE, IN SCROLL ORDER",
        sections,
    ]

    if spec.highlights:
        out += [
            "",
            "HIGHLIGHTS STRIP (directly under the hero, one row of badges)",
            *(f"- {h}" for h in spec.highlights),
        ]

    if spec.showcase_items:
        out += [
            "",
            "WHAT THEY OFFER (build a card for each, in this order)",
            *(f"{i}. {item}" for i, item in enumerate(spec.showcase_items, start=1)),
            "",
        ]
        # IMAGERY only renders when there is a source to pull photos from --
        # pointing at it here when there is none would send the builder to a
        # section that does not exist. No sources is exactly the case a
        # tinted block exists for: an honest placeholder beats a fabricated
        # photo.
        if sources:
            out.append(
                "Each card: image area, name, its tags as small badges, and a tap "
                "target opening a modal with a little more detail. Follow IMAGERY "
                "above for the photo -- never a stock photo."
            )
        else:
            out.append(
                "Each card: image area, name, its tags as small badges, and a tap "
                "target opening a modal with a little more detail. No source was "
                "given to pull a photo from, so use a solid tinted block from the "
                "palette for the image area -- never a stock photo."
            )

    if spec.interactive_feature:
        out += [
            "",
            "THE INTERACTIVE PIECE (build this properly -- it is the thing that",
            "makes the site feel made for them)",
            spec.interactive_feature,
            "",
            "It must work with a thumb, hold its state as they move through it, "
            "show what they have chosen so far, and finish by handing them to "
            f'"{spec.primary_cta}" with their choices already filled in.',
        ]

    if spec.proof_points:
        out += [
            "",
            "PROOF (all verified -- show these, invent nothing further)",
            *(f"- {p}" for p in spec.proof_points),
        ]

    out += ["", "ART DIRECTION"]
    if spec.palette:
        out.append(f"Palette: {spec.palette}")
    if spec.typography:
        out.append(f"Type:    {spec.typography}")
    if spec.visual_direction:
        out.append(f"Feel:    {spec.visual_direction}")

    motion = []
    if spec.signature_moment:
        motion += [f"Signature moment: {spec.signature_moment}"]
    if spec.motion_language:
        motion += [f"Motion language:   {spec.motion_language}"]
    if spec.micro_interactions:
        motion += ["Micro-interactions:", *(f"  - {m}" for m in spec.micro_interactions)]

    # Rendered even when the spec supplied nothing bespoke: a thin spec must
    # not silently ship a static page. The floor list below is fixed text,
    # not derived from spec values, so it survives micro_interactions=None
    # and an empty signature_moment alike.
    # Items 1-3 are the floor for every site -- they don't depend on which
    # optional sections the spec asked for. 4 and 5 name a showcase card and
    # the mobile bar specifically, so they only belong on the list when the
    # spec actually has one; naming them unconditionally told the builder to
    # invent both, straight against "if a section does not move someone
    # toward the CTA, it does not belong on the page" a few paragraphs up.
    floor_items = [
        "The signature moment above, or the closest fit if none was given.",
        "Sections below the fold reveal on scroll, staggered sibling to sibling.",
        "The primary CTA answers the tap: a scale or opacity change on press,"
        " not only on hover, which does not exist on a phone.",
    ]
    if spec.showcase_items:
        floor_items.append(
            "A showcase card expands into its modal as a shared-element move,"
            " not a hard cut."
        )
    if spec.mobile_bar:
        floor_items.append(
            "The mobile bar enters with its own animation once the visitor has"
            " scrolled past the hero -- it is not present from frame one."
        )

    out += ["", "MOTION AND INTERACTION"]
    if motion:
        out += motion
    out += [
        "",
        "Stack: React + Tailwind + shadcn/ui. Framer Motion is already available"
        " -- use motion.* directly. whileInView with viewport={{ once: true }}"
        " for on-scroll reveals, AnimatePresence for the showcase modal,"
        " layoutId for the card-to-modal shared-element move,"
        " useReducedMotion() as the a11y fallback. Spring for a snappy"
        " motion_language, tween for a slow one. Reach for shadcn's Dialog,"
        " Sheet, Tabs and Accordion over hand-rolled equivalents -- they"
        " already handle focus and escape.",
        "",
        "BUILD ALL OF THESE -- a page with none of them is not a pass:",
        *(f"{i}. {item}" for i, item in enumerate(floor_items, start=1)),
        "",
        "The hero itself does not animate: final position, full opacity, no"
        " entrance, no load gate, the instant the page paints. That alone"
        " protects the performance budget below -- everything after the hero"
        " is free to move.",
        "",
        "- Animate transform and opacity only -- width, height, top or left"
        " force layout on every frame and stutter on a mid-range phone.",
        "- Reveal on scroll with whileInView, viewport={{ once: true }};"
        " never re-trigger. Content is in the DOM and readable even if the animation never runs.",
        "- Honour prefers-reduced-motion via useReducedMotion(): plain fade,"
        " or nothing. Some people get motion sickness from parallax.",
        "- No scroll-jacking, no page-load animation gate, no cursor effects,"
        " no WebGL or Three.js. Those belong on a designer's portfolio, not a"
        " shop that needs the booking -- and the cursor ones do not exist on"
        " a phone.",
    ]

    if spec.avoid:
        out += [
            "",
            "DO NOT (specific to this business)",
            *(f"- {a}" for a in spec.avoid),
        ]

    contact = []
    if phone:
        contact.append(f"Phone: {phone} -- as a tappable tel: link, in the header")
    if address:
        contact.append(f"Address: {address} -- with a link to Google Maps")
    if spec.mobile_bar:
        out += [
            "",
            "FLOATING BAR ON MOBILE (fixed to the bottom of the screen, always",
            "visible, above everything else)",
            *(f"- {action}" for action in spec.mobile_bar),
            "",
            "This is where most phone conversions happen. It must never cover "
            "the last line of content -- pad the page bottom by its height.",
        ]

    if contact:
        out += ["", "REAL DETAILS (use exactly these, do not paraphrase)", *contact]

    profiles = [line.strip() for line in social_profiles.splitlines() if line.strip()]
    if profiles:
        out += [
            "",
            "SOCIAL PROFILES (link every one, using these exact URLs)",
            *(f"- {line}" for line in profiles),
            "",
            "Put them where a customer would look: in the header or the footer,"
            " as the platform's own icon, opening in a new tab. These are the"
            " only social links that may appear on the site -- do not invent a"
            " handle for a platform that is not listed here, and do not guess a"
            " URL from the business name.",
        ]
    # A bare handle with no URL is deliberately not rendered here, even as
    # unlinked text: "@sun -- link it prominently" with nothing to link is an
    # invitation for the builder to guess a URL, and a guessed URL can send a
    # real customer to a stranger's profile. `social_profiles` (backed by
    # `social.profiles_text`/a real lookup) is the only path a link reaches
    # this prompt by.

    out += [
        "",
        "BUILD RULES",
        "- Mobile first, and thumb first. Assume a mid-range Android on a patchy",
        "  connection. Large tap targets, nothing that depends on hover, nothing",
        "  important more than three scrolls down.",
        "- Fast and light. No carousels, no popups, no cookie banner theatre, no",
        "  stock photos of models who have never been to this business.",
        "- Every claim must be one the owner can stand behind. Do not invent",
        "  prices, awards, testimonials, staff names, or years in business.",
        "- Where a real detail is needed but was not supplied, write it as",
        "  [TO CONFIRM] rather than guessing. That is the correct outcome.",
        "- Accessible by default: real contrast, real heading order, alt text,",
        "  and a form every keyboard can complete.",
        "- Performance budget governs the hero and the first tap, not the"
        " rest of the page: largest contentful paint under 2.5s on a"
        " mid-range Android on 4G, no layout shift as things load, primary"
        " action tappable within one second. The hero renders at final"
        " position and full opacity with no entrance animation and no load"
        " gate -- that alone protects the budget. Below the fold, animate"
        " freely inside the transform/opacity rule above. If an effect cannot"
        " fit inside the hero-and-first-tap budget, drop the effect -- not"
        " the budget.",
        "- Real images beat illustrations, but they must be sized and lazy",
        "  loaded below the fold, with width and height set so nothing jumps.",
        "- Send every booking or enquiry to a spreadsheet via the",
        "  Google Sheets connector, one row per enquiry with a timestamp.",
        "  This owner has never had a website and will not log into an",
        "  admin panel to check messages. A spreadsheet is a tool they already know.",
        "- Put a WhatsApp click-to-chat link next to any form. In this market that is",
        "  how people actually get in touch, and a form alone will lose enquiries.",
    ]

    return "\n".join(out) + "\n"


def build_site_spec_prompt(
    *,
    name: str,
    category: str,
    location: str,
    tone: str,
    services: str,
    price_tier: str,
    praise: str,
    pain_points: str,
    angle: str,
    social_handle: str = "",
    social_bio: str = "",
    social_post: str = "",
    social_profiles: str = "",
    phone: str = "",
    address: str = "",
) -> str:
    lines = [
        f"Business: {name}",
        f"Category: {category or 'unknown'}",
        f"Location: {location or 'unknown'}",
    ]
    if address:
        lines.append(f"Address: {address}")
    if phone:
        lines.append(f"Phone: {phone}")

    lines += [
        "",
        "Research brief (this is all the evidence there is -- anything not "
        "here is not known):",
        f"- Tone: {tone or 'unknown'}",
        f"- Services: {services or 'unknown'}",
        f"- Price tier: {price_tier or 'unknown'}",
        f"- Customers praise: {praise or 'nothing recorded'}",
        f"- Customer pain points: {pain_points or 'none evident'}",
        f"- Suggested angle: {angle or 'none recorded'}",
    ]

    # The social profile matters twice over: it's the business's own voice
    # (better evidence of tone than anything inferred from reviews), and the
    # site should link to it rather than compete with it.
    if social_profiles or social_handle or social_bio or social_post:
        lines += ["", "Their own social presence:"]
        if social_profiles:
            lines.append("- Profiles (platform, handle, link):")
            lines += [f"    {line}" for line in social_profiles.splitlines() if line.strip()]
        elif social_handle:
            lines.append(f"- Handle: {social_handle}")
        if social_bio:
            lines.append(f"- Bio: {social_bio}")
        if social_post:
            lines.append(f"- Recent post: {social_post}")
        lines.append(
            "- Link every one of these prominently on the site. This is where "
            "their existing customers already follow them, and their photo "
            "feed is content the site would otherwise lack."
        )
    else:
        lines += ["", "No social profile found -- do not invent or link one."]

    return "\n".join(lines)


def build_outreach_prompt(
    *,
    name: str,
    category: str,
    location: str,
    tone: str,
    services: str,
    praise: str,
    pain_points: str,
    angle: str,
    sender_name: str,
    sender_title: str,
    company: str,
    preview_url: str = "",
) -> str:
    lines = [
        f"Business: {name}",
        f"Category: {category or 'unknown'}",
        f"Location: {location or 'unknown'}",
        "",
        "Research brief:",
        f"- Tone: {tone or 'unknown'}",
        f"- Services: {services or 'unknown'}",
        f"- Customers praise: {praise or 'nothing recorded'}",
        f"- Customer pain points: {pain_points or 'none evident'}",
        f"- Website angle: {angle or 'none recorded'}",
        "",
        "Sender:",
        f"- Name: {sender_name or 'the sender'}",
        f"- Title: {sender_title or ''}",
        f"- Company: {company or ''}",
    ]
    if preview_url:
        lines += [
            "",
            f"A working preview site has already been built: {preview_url}",
            "Lead with that -- it is the strongest thing in the message.",
        ]
    else:
        lines += [
            "",
            "No site has been built yet. Do not imply one exists or offer a link.",
        ]
    return "\n".join(lines)


def build_prompt(
    *,
    name: str,
    category: str,
    address: str,
    rating: float | None,
    review_count: int,
    price_level: str,
    editorial_summary: str,
    opening_hours: tuple[str, ...] | list[str],
    reviews: list[tuple[int, str, str]],
    social_bio: str = "",
    social_post: str = "",
    website_status: str = "none",
) -> str:
    """Assemble the evidence block. Deterministic, so it is easy to test."""
    lines = [
        f"Business name: {name}",
        f"Category: {category or 'unknown'}",
        f"Address: {address or 'unknown'}",
        f"Google rating: {rating if rating is not None else 'none'} "
        f"from {review_count} reviews",
        f"Google price level: {price_level or 'not stated'}",
        f"Existing web presence: {website_status}",
    ]
    if opening_hours:
        lines.append("Opening hours: " + "; ".join(opening_hours))
    if editorial_summary:
        lines.append(f"Google summary: {editorial_summary}")
    if social_bio:
        lines.append(f"Social bio: {social_bio}")
    if social_post:
        lines.append(f"Recent social text: {social_post}")

    if reviews:
        lines.append("")
        lines.append("Reviews:")
        for stars, text, when in reviews:
            when_part = f", {when}" if when else ""
            lines.append(f"- [{stars}/5{when_part}] {text}")
    else:
        lines.append("")
        lines.append("Reviews: none available.")

    return "\n".join(lines)


def _extract_json(response) -> dict:
    """Pull the structured payload out of the response, checking stop_reason.

    Claude Opus 5 can decline a request outright -- checking ``stop_reason``
    before touching ``content`` is what stops that becoming an IndexError.
    """
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise LLMError(f"Model declined this request (category: {category or 'unknown'})")
    if stop_reason == "max_tokens":
        raise LLMError("Response hit max_tokens before completing the brief")

    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"Model returned unparseable JSON: {exc}") from exc
    raise LLMError(f"No text block in response (stop_reason={stop_reason})")


def _to_brief(payload: dict) -> ResearchBrief:
    services = payload.get("services") or []
    if isinstance(services, str):
        services = [services]
    return ResearchBrief(
        tone=str(payload.get("tone", "")).strip(),
        services=tuple(str(s).strip() for s in services if str(s).strip()),
        price_tier=str(payload.get("price_tier", "unknown")).strip(),
        customer_praise=str(payload.get("customer_praise", "")).strip(),
        customer_pain_points=str(payload.get("customer_pain_points", "")).strip(),
        suggested_website_angle=str(payload.get("suggested_website_angle", "")).strip(),
    )


class _Writer:
    """Everything the stages use, independent of which model answers.

    Subclasses implement ``_call`` only. Keeping the parsing here means a
    switch of provider cannot quietly change the shape of a brief.
    """

    model = ""
    call_count = 0

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> dict:
        raise NotImplementedError

    def write(self, prompt: str) -> ResearchBrief:
        """Stage 2 -- the research brief."""
        return _to_brief(self._call(SYSTEM_PROMPT, prompt, BRIEF_SCHEMA))

    def write_site_spec(self, prompt: str) -> SiteSpec:
        """Stage 4 -- the structured site spec behind the Lovable prompt."""
        payload = self._call(SITE_SPEC_SYSTEM, prompt, SITE_SPEC_SCHEMA)
        return SiteSpec(
            site_goal=str(payload.get("site_goal", "")).strip(),
            pages=_as_tuple(payload.get("pages")),
            hero_headline=str(payload.get("hero_headline", "")).strip(),
            hero_subcopy=str(payload.get("hero_subcopy", "")).strip(),
            key_sections=_as_tuple(payload.get("key_sections")),
            primary_cta=str(payload.get("primary_cta", "")).strip(),
            visual_direction=str(payload.get("visual_direction", "")).strip(),
            visitor=str(payload.get("visitor", "")).strip(),
            proof_points=_as_tuple(payload.get("proof_points")),
            palette=str(payload.get("palette", "")).strip(),
            typography=str(payload.get("typography", "")).strip(),
            avoid=_as_tuple(payload.get("avoid")),
            highlights=_as_tuple(payload.get("highlights")),
            showcase_items=_as_tuple(payload.get("showcase_items")),
            interactive_feature=str(payload.get("interactive_feature", "")).strip(),
            mobile_bar=_as_tuple(payload.get("mobile_bar")),
            signature_moment=str(payload.get("signature_moment", "")).strip(),
            motion_language=str(payload.get("motion_language", "")).strip(),
            micro_interactions=_as_tuple(payload.get("micro_interactions")),
        )

    def write_outreach_copy(self, prompt: str) -> OutreachCopy:
        """Stage 5 -- the email and the WhatsApp opener."""
        payload = self._call(OUTREACH_COPY_SYSTEM, prompt, OUTREACH_COPY_SCHEMA)
        return OutreachCopy(
            subject=str(payload.get("subject", "")).strip(),
            email_body=str(payload.get("email_body", "")).strip(),
            whatsapp_message=str(payload.get("whatsapp_message", "")).strip(),
        )


class BriefWriter(_Writer):
    """Wraps the Anthropic client so stages don't import the SDK directly."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-opus-5",
        effort: str = "medium",
        max_tokens: int = 6000,
        client: Any | None = None,
    ) -> None:
        if client is None:
            if not api_key:
                raise LLMError(
                    "ANTHROPIC_API_KEY is empty. Set it in .env -- Stage 2 needs it "
                    "to write research briefs."
                )
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover -- declared dependency
                raise LLMError("The `anthropic` package is not installed") from exc
            client = anthropic.Anthropic(api_key=api_key)

        self._client = client
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.call_count = 0
        self._warned_effort = False

    def _output_config(self, schema: dict[str, Any]) -> dict[str, Any]:
        config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if supports_effort(self.model):
            config["effort"] = self.effort
        elif not self._warned_effort:
            log.info(
                "%s does not support the effort parameter -- ignoring "
                "research.effort=%s for this model.",
                self.model, self.effort,
            )
            self._warned_effort = True
        return config

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> dict:
        self.call_count += 1
        try:
            response = self._client.messages.create(
                model=self.model,
                # Thinking is on by default on Opus 5 and counts against
                # max_tokens, so the budget below leaves room for both.
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=self._output_config(schema),
            )
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 -- surfaces as a per-lead error
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        return _extract_json(response)


class OpenAICompatibleWriter(_Writer):
    """Ollama (local or cloud), OpenRouter, Gemini, or anything speaking that API.

    The important difference from Anthropic is not the transport, it is the
    guarantee. Anthropic's structured outputs are enforced -- the response
    matches the schema or the call fails. Here, ``response_format`` is honoured
    natively by some backends and treated as a strong hint by others, and a
    local model may simply ignore it and reply in prose.

    So the schema is checked here rather than trusted: a reply missing required
    fields gets one corrective retry naming exactly what was absent, and after
    that the lead fails cleanly. Writing a half-filled brief to the Sheet would
    be worse than not researching that business at all -- it becomes a demo
    site with holes in it and an email built on nothing.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        max_tokens: int = 6000,
        timeout: float = 180.0,
        session: Any | None = None,
    ) -> None:
        if not base_url:
            raise LLMError("No base_url for this provider -- set research.base_url.")

        import requests

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.call_count = 0
        self._session = session or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _post(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            # Ask for schema conformance where the backend can enforce it.
            # Backends that cannot will ignore this, which is what the
            # validation below is for.
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "brief", "strict": True, "schema": schema},
            },
        }
        url = f"{self.base_url}/chat/completions"
        try:
            response = self._session.post(
                url, json=body, headers=self._headers, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001 -- per-lead error, not a crash
            raise LLMError(f"Could not reach {url}: {type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(_explain_http(response.status_code, response.text, url))

        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"{self.model} returned a response in an unexpected shape: {exc}"
            ) from exc

        return _reply_text(message)

    def _call(self, system: str, prompt: str, schema: dict[str, Any]) -> dict:
        self.call_count += 1
        messages = [
            {"role": "system", "content": system + "\n\n" + _json_instruction(schema)},
            {"role": "user", "content": prompt},
        ]

        text = self._post(messages, schema)
        try:
            return _validated(text, schema)
        except LLMError as first:
            log.info("  %s: %s -- asking it to correct itself once.", self.model, first)

        # One repair attempt. Models that ignored the schema usually comply
        # when told precisely what was wrong; a second failure is a real one.
        messages += [
            {"role": "assistant", "content": text[:2000]},
            {
                "role": "user",
                "content": (
                    "That was not valid. Reply with JSON only -- no prose, no "
                    "markdown fences -- containing exactly these keys: "
                    + ", ".join(schema.get("required", []))
                ),
            },
        ]
        return _validated(self._post(messages, schema), schema)


def _reply_text(message: dict) -> str:
    """Prefer ``content``; fall back to a reasoning field when it is empty.

    DeepSeek's reasoning variants (the r1 family and its descendants) can put
    the whole answer in ``reasoning_content`` -- or plain ``reasoning`` on some
    backends -- and return an empty ``content`` when the output budget runs
    out before a separate answer gets written. Discarding that field turned a
    real reply into "reply was not JSON (char 0)": the model had answered, just
    not in the field the plain OpenAI wire shape expects. Any of these can be
    absent, ``None``, or not a string, so each is checked before use.
    """
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return content if isinstance(content, str) else ""


def _json_instruction(schema: dict[str, Any]) -> str:
    """Belt and braces for backends that ignore ``response_format``."""
    return (
        "Reply with a single JSON object and nothing else -- no prose, no "
        "explanation, no markdown code fences. It must contain exactly these "
        "keys: " + ", ".join(schema.get("required", [])) + ".\n\n"
        "Schema:\n" + json.dumps(schema.get("properties", {}), indent=2)
    )


# Matches a *complete* <think>...</think> or <reasoning>...</reasoning>
# block. The backreference keeps an open <think> from being closed by a
# stray </reasoning> or vice versa. Deliberately does not match an
# unterminated tag -- see the comment in _strip_fences for why that matters.
_THINK_BLOCK = re.compile(r"<(think|reasoning)>.*?</\1>", re.IGNORECASE | re.DOTALL)


def _strip_fences(text: str) -> str:
    """Models love wrapping JSON in ```json fences, or a <think> block. Unwrap both."""
    cleaned = (text or "").strip()
    # Strip well-formed thinking blocks first, before hunting for braces.
    # Reasoning text routinely contains a stray brace of its own -- models
    # write things like `I'll return {"visitor": ...}` while thinking aloud --
    # and the brace-hunting below would grab a span starting inside the
    # reasoning instead of the real answer. If the tag is never closed, the
    # regex simply doesn't match and nothing is stripped: an unterminated
    # <think> must not be treated as "the rest of the reply is thinking" and
    # eaten wholesale, because the real answer may still be sitting right
    # after it.
    cleaned = _THINK_BLOCK.sub("", cleaned).strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    # Some models still prepend a sentence. Take the outermost JSON object.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return cleaned.strip()


def _snippet(text: str, limit: int = 200) -> str:
    """A short, safe-to-log slice of what the model actually said."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _validated(text: str, schema: dict[str, Any]) -> dict:
    """Parse and check required keys. Raises ``LLMError`` naming what is wrong.

    Three distinct failures get three distinct messages, because "reply was
    not JSON (char 0)" told Lyon nothing when the actual cause was an empty
    reply -- a reasoning model can spend its whole output budget thinking and
    return nothing at all, which is a config problem (model choice or output
    limit), not a prompt problem, and needs a different fix than a model that
    replied in prose or one that got the JSON almost right.
    """
    raw = (text or "").strip()
    if not raw:
        required_n = len(schema.get("required", []))
        # Both counts are read from the schemas. A number typed into a string
        # here would keep being printed long after the schema moved, and this
        # message exists to be trusted at 7am when a batch has half failed.
        worst_n = len(SITE_SPEC_SCHEMA.get("required", []))
        harder_call = (
            ""
            if required_n >= worst_n
            else (
                f" The site spec is the worst of it, at {worst_n} required "
                "fields in one shot, so a model that survives this call can "
                "still fail that one."
            )
        )
        raise LLMError(
            "the model returned nothing at all -- no content, and no "
            f"reasoning text either. This call needs {required_n} fields "
            "back, and an empty reply like this is usually a reasoning model "
            "spending its whole output budget on chain-of-thought before it "
            f"gets to an answer.{harder_call} Two fixes: switch to a "
            "non-reasoning model, or raise the context/output limit. Use "
            "`test-model` in the dashboard to check a model before running "
            "a batch."
        )

    cleaned = _strip_fences(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        if "{" in cleaned or "}" in cleaned:
            raise LLMError(
                f"reply had JSON that failed to parse ({exc}): {_snippet(raw)}"
            ) from exc
        raise LLMError(
            "the model replied in prose, with no JSON object anywhere in "
            f"it: {_snippet(raw)}"
        ) from exc

    if not isinstance(payload, dict):
        raise LLMError("reply was JSON but not an object")

    missing = [
        key
        for key in schema.get("required", [])
        if key not in payload or payload[key] in (None, "")
    ]
    if missing:
        raise LLMError(f"reply was missing required field(s): {', '.join(missing)}")
    return payload


def _explain_http(status: int, body: str, url: str) -> str:
    snippet = (body or "")[:300]
    if status in (401, 403):
        return (
            f"{url} rejected the API key (HTTP {status}). Check the key in .env "
            "matches the provider in your campaign file -- OLLAMA_API_KEY for "
            "ollama-cloud, OPENROUTER_API_KEY for openrouter, GEMINI_API_KEY "
            "for gemini.\n" + snippet
        )
    if status == 404:
        return (
            f"{url} returned 404. Usually the model name is wrong or not pulled "
            f"yet. For local Ollama try `ollama pull <model>` first.\n{snippet}"
        )
    if status == 429:
        return f"Rate limited by {url} (HTTP 429).\n{snippet}"
    return f"{url} returned HTTP {status}.\n{snippet}"


def make_writer(config: Any) -> _Writer:
    """Build the writer this campaign asks for.

    One place decides, so every stage gets the same model without each of them
    knowing which provider is in play.
    """
    provider = config.research.provider
    if provider == "anthropic":
        return BriefWriter(
            config.secrets.anthropic_api_key,
            model=config.research.model,
            effort=config.research.effort,
        )

    from ..config import PROVIDER_KEY_ENV

    key = config.secrets.llm_api_key
    if provider != "ollama" and not key:
        raise LLMError(
            f"research.provider is {provider!r} but "
            f"{PROVIDER_KEY_ENV.get(provider, 'the API key')} is not set in .env."
        )
    return OpenAICompatibleWriter(
        key, base_url=config.research.base_url, model=config.research.model
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in (value or []) if str(v).strip())
