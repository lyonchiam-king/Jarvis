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
from collections.abc import Sequence
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
        # Deliberately NOT in `required`. The cap of 19 required fields is a
        # ratchet: a 20th costs every weaker non-Anthropic model the whole
        # spec, not just this field. Optional degrades instead -- a model that
        # fills it gets a designed layout, one that skips it renders exactly
        # what it rendered before, because the prompt block is conditional.
        "layout_system": {
            "type": "string",
            "description": (
                "The structure, decided rather than defaulted: content max-width "
                "in px, how many columns and which sections deliberately break "
                "out of them (full-bleed, offset, overlapping), the gutter, the "
                "spacing scale as actual numbers, the corner radius language, "
                "and whether edges are borders, shadow, or neither. This is the "
                "field that stops every site looking alike, so derive it from "
                "the business: a tailor earns an asymmetric editorial split with "
                "generous margins, a tuition centre a dense readable grid, a "
                "hawker stall full-bleed slabs with almost no chrome. If your "
                "answer would suit all three, you have not designed it."
            ),
        },
        "palette": {
            "type": "string",
            "description": (
                "Actual decisions with hex values, named as tokens and given "
                "their job: --bg, --surface, --text, --muted, --accent. Include "
                "the accent's hover and pressed variants, and confirm body text "
                "on background clears 4.5:1. Justify the choice from the "
                "business, not from taste. Three or four colours total -- more "
                "is a sign of an undecided design."
            ),
        },
        "typography": {
            "type": "string",
            "description": (
                "Named typefaces available on Google Fonts, one for headings "
                "and one for body, each with a real fallback stack. Give the "
                "scale as numbers -- the body size, the ratio between steps, "
                "and the clamp() range the hero headline moves through between "
                "phone and desktop -- plus the weights used. Say why this "
                "pairing suits this business."
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
                "One idea, achievable in CSS, and it must not "
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

**Decide the structure, do not inherit it.** Colour and type are the easy half; \
two sites with different palettes and the same layout still look like the same \
site. The layout is what a visitor reads as "someone designed this": where the \
grid holds and where a section deliberately breaks out of it, how much air sits \
around the content, whether edges are drawn with a border, a shadow, or \
nothing at all. Give `layout_system` real numbers, and make them follow from \
this business -- a jeweller and a tyre shop should not resolve to the same \
1100px centred column with cards in it.

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

AREAS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "areas": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Search areas, each written the way someone would type it "
                "into Google Maps: the district or suburb, then the "
                "city/state, then the country. Ordered densest-first, "
                "because discovery drinks the wells in order."
            ),
        }
    },
    "required": ["areas"],
    "additionalProperties": False,
}

AREAS_SYSTEM = """\
You list the districts, suburbs and neighbouring towns that make up a local \
business market, so a lead-discovery run can work through them one at a time.

Google's text search returns at most about 60 results for any one query, \
however you paginate. That is the whole reason this list exists: one broad \
query like "New Orleans" is a single 60-result well, while twenty named \
neighbourhoods are twenty wells. So name real, distinct, adjacent places -- \
never slice one place into invented sub-parts.

Rules:
- Write each area the way it would be typed into Google Maps, with enough \
  administrative context to be unambiguous: "Bywater, New Orleans, LA, USA", \
  not "Bywater".
- Real places only. If you are unsure a neighbourhood exists under that name, \
  leave it out. A made-up area returns nothing and wastes a run.
- Cover the requested place properly before straying outside it, then include \
  genuinely adjacent towns that share the same market.
- Order by how many small businesses each is likely to hold, densest first.
- No duplicates, and never repeat the parent location as its own area.
"""


def build_areas_prompt(location: str, count: int) -> str:
    return (
        f"List up to {count} search areas covering {location}.\n\n"
        "These are for finding small local businesses -- salons, cafes, "
        "workshops, clinics -- so favour the commercial and residential "
        "districts where such businesses actually sit over industrial parks "
        "or open country."
    )


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
                "Short paragraphs separated by blank lines, never one wall of "
                "text. Under 150 words."
            ),
        },
        "whatsapp_message": {
            "type": "string",
            "description": (
                "A much shorter WhatsApp opener for the same business. Open "
                "with 'Hi, [your name] here,'. Short lines separated by blank "
                "lines, with the preview link on its own line as "
                "'Website: <link>'. Under 60 words, conversational, no subject "
                "line, no formal signoff."
            ),
        },
    },
    "required": ["subject", "email_body", "whatsapp_message"],
    "additionalProperties": False,
}

_COPY_PREAMBLE = """\
You write first-contact messages from a founder to a small business owner who \
has never heard of him. Professional but warm -- the register of a competent \
neighbour, not a marketing department.

**Structure every message as hook, solution, offer, model, in that order.** \
Four short paragraphs, a blank line between each, and nothing else.

"""

# The original argument. The business has no website, so the demo site is the
# whole pitch: they cannot picture one, and being shown theirs closes that gap
# in a way no sentence does.
_BEATS_NO_WEBSITE = """\
1. HOOK -- one or two sentences that prove you looked at THEIR business \
before you wrote, and that land on the gap. Name the specific thing: what \
customers keep praising, the complaint that repeats in the reviews, the thing \
they do that nobody else nearby does -- then the consequence, which is that \
the people who already found them rate them highly and the people who have \
not cannot find them at all. The specific detail is evidence, not a \
compliment; it is there to make the next sentence about customers they are \
missing rather than about you. This is the only sentence that decides whether \
the rest is read, so it must be impossible to send to any other business. \
"I hope this finds you well" and "I came across your business" are deleted \
unread.

2. SOLUTION -- what you did about it, in plain words. Not what you could do, \
not what you offer in general: the specific thing, tied to what the hook just \
named. One or two sentences. The subject is their customers, not your work: \
more of the people the hook just described, found by the ones who cannot find \
them yet. The site is how you are showing that rather than describing it, so \
say what it does for them -- never that you "built a website", which is a \
sentence about you.

3. OFFER -- the concrete thing they get, and what it costs them to look. When \
a preview site exists, this is where the link goes, on its own line with a \
blank line either side, written as "Website: [the link]". The offer is always \
small: look at it, tell me what is wrong with it. Never a meeting.

4. MODEL -- how this works in one sentence, so nobody has to guess what the \
catch is. Say plainly what happens next if they like it and what happens if \
they do not. Ending without this reads as a trick, and a small business owner \
who suspects a trick does not reply. Then one clause -- no list -- saying \
getting found is the wider job and the site is one part of it. That is what \
the appended list of services follows on from, so without it the list arrives \
out of nowhere; with it, the message ends on their growth rather than on a \
yes-or-no about one website. A list of services is appended after your \
message, worded by the sender, so writing one here duplicates it and risks \
naming something nobody sells.
"""

# For a business that already has a site. "Do you need a website" puts you in
# a queue behind every web designer and invites a no from anyone with a page
# they paid for. "Visitors arrive and do not enquire" is a different question,
# about money rather than about design, and it is one the owner has usually
# already half-asked themselves.
_BEATS_CONVERSION = """\
1. HOOK -- one or two sentences that prove you looked at THEIR business \
before you wrote, and that land on the gap between being found and being \
contacted. Name the specific thing -- what customers keep praising, what they \
do that nobody else nearby does -- then the consequence: the people who \
already reached them rate them highly, and the people who land on their page \
and leave without getting in touch are the whole opportunity. Never criticise \
their website. Someone paid for it, quite possibly them, and an insult is a \
reason to stop reading. The subject is the visitor who did not call, not the \
site that failed them.

2. SOLUTION -- what you did about it, in plain words. One page, built for one \
action -- calling, messaging, or booking -- so that a visitor who is already \
interested has one obvious thing to do. Tie it to what the hook named. The \
subject is their enquiries, not your work: never "I built you a landing \
page", which is a sentence about you. Do not describe their existing site as \
replaced; this sits alongside it, for the people it is losing.

3. OFFER -- the concrete thing they get, and what it costs them to look. When \
a preview page exists, this is where the link goes, on its own line with a \
blank line either side, written as "Page: [the link]". The offer is always \
small: look at it, tell me what is wrong with it. Never a meeting.

4. MODEL -- how this works in one sentence, so nobody has to guess what the \
catch is. Say plainly what happens next if they like it and what happens if \
they do not. Ending without this reads as a trick, and a small business owner \
who suspects a trick does not reply. Then one clause -- no list -- saying \
turning interest into enquiries is the wider job and the page is one part of \
it. That is what the appended list of services follows on from, so without it \
the list arrives out of nowhere. A list of services is appended after your \
message, worded by the sender, so writing one here duplicates it and risks \
naming something nobody sells.
"""

# The infographic track. The artefact is not a page they click -- it is the
# picture attached to the message, so the beats have to put that picture at the
# centre instead of a link. Written as its own block rather than by editing the
# two above, for the reason the conversion angle was: adding a track must not
# change a single word of the prompt the other tracks already produce.
_BEATS_VISUAL = """\
1. HOOK -- one or two sentences that prove you looked at THEIR business \
before you wrote, and that land on the first point the attached picture \
makes, which is listed for you in the brief. \
Name the specific thing -- what customers keep praising, what they do that \
nobody else nearby does -- then the consequence, which is about the customers \
they are missing, never about you. This is the only sentence that decides \
whether the rest is read, so it must be impossible to send to any other \
business. "I hope this finds you well" and "I came across your business" are \
deleted unread.

2. WHAT IS ATTACHED -- one or two sentences on what the picture shows: the \
few things costing them enquiries, and what the businesses doing well do \
instead, on one page they can read at a glance. The subject is their \
customers, not your work. Never say you "made a graphic" or "designed an \
infographic" -- that is a sentence about you -- and never offer to build \
anything here. Do not list the items; they are already in the picture, and \
repeating them in text makes the message long and the picture pointless.

3. VALUE -- the picture is theirs, and this sentence says so. It is attached, \
it costs them nothing, and the fixes on it are ones they could do themselves. \
Say that plainly. Do not ask them to critique it, do not ask which item is \
wrong, and do not ask for anything at all in this paragraph: a message that \
arrives with something useful and no request is the reason this one gets \
read, and spending it on a question makes it another pitch.

4. INVITATION -- one sentence, low and open: if any of it is useful, or they \
want to know how a particular one is done, they can message back and you will \
explain it. That is the whole ask. No meeting, no call, no form, no deadline, \
and nothing they have to decide. Then one clause -- no list -- saying getting \
found is the wider job and this is one part of it. That is what the appended \
list of services follows on from, so without it the list arrives out of \
nowhere. A list of services is appended after your message, worded by the \
sender, so writing one here duplicates it and risks naming something nobody \
sells.

RULE ABOUT A WEBSITE: say nothing about a website, a page, or a link unless \
the brief explicitly tells you one exists and to include it. This message \
offers a picture and the thinking behind it, not a build. A lead may well \
have been sent a site months ago -- that is not this message's subject and \
raising it reads as a second attempt at the same sale.
"""

# Added to the visual beats when this lead also has a built page. "Both"
# produces ONE message, not two: the same business messaged twice in a day is
# the thing this pipeline works hardest to avoid, so the picture is the hook
# and the page rides in the value paragraph beside it.
_VISUAL_PLUS_PAGE = """\
- This lead also has a working page built for them. Put its link in the VALUE \
paragraph, on its own line with a blank line either side, written as \
"Website: [the link]", AFTER the sentence saying the picture is theirs. The \
picture is the hook and the page is the proof; a message that leads with the \
link throws the picture away. Mention the page in one clause only -- the \
picture is still the subject.
"""

# The graphic carries no business name and no claim about them specifically,
# because the research could not support three. The message must say so, in
# those terms: a generic picture under a sentence claiming you studied their
# business is a lie the owner can spot from the picture itself.
_VISUAL_GENERIC_LINE = """\
- The attached picture is NOT specific to this business: it names three gaps \
that are common across businesses like theirs. So the solution paragraph must \
say that plainly -- the things you see again and again in this trade -- and \
must never claim the picture was made for them, built from their reviews, or \
based on looking at their business. The hook may still be specific, because \
it is written from the brief; the picture may not be described as such.
"""

# Only added when the campaign says this is genuinely on offer. A message
# promising follow-up nobody has built is a promise about future work rather
# than a description of the thing already made -- the same failure as an
# invented service, and worse, because it is the reason they replied.
_FOLLOW_UP_LINE = """\
- The sender also sets up follow-up after an enquiry -- an automatic message \
so nobody who gets in touch is forgotten. You may mention this once, in the \
model paragraph, in a single clause. Never as the main offer, never with a \
number attached to it.
"""

_NO_FOLLOW_UP_LINE = """\
- Never promise follow-up, automation, reminders, a CRM, or anything that \
happens after an enquiry arrives. This campaign does not offer it, and a \
message that promises it is selling something nobody is ready to deliver.
"""

_COPY_RULES = """\
Rules:
- Open the WhatsApp message with "Hi, [your name] here," -- introduce yourself \
by the name given in the Sender block, then straight into the hook.
- Never list, name or number the sender's other services. They are appended \
verbatim after your message. Naming them yourself gets one reworded, one \
dropped, or a fourth invented that nobody sells.
- One ask, and make it small -- a reply, not a meeting.
- No hype, no superlatives, no "revolutionise", no exclamation marks.
- Never claim you are an existing customer, never invent a mutual connection, \
never imply a prior relationship. Everything you say must be true from the \
brief alone.
- Never invent a price, a discount, a deadline or a guarantee. If the model \
paragraph needs a commercial detail you were not given, say what happens \
rather than what it costs.
- The WhatsApp version is not the email pasted shorter -- it is a message a \
person would actually send on their phone. It keeps all four beats, but each \
one is a line rather than a paragraph.
"""


def outreach_copy_system(
    angle: str = "no-website",
    *,
    offers_follow_up: bool = False,
    pitch: str = "site",
    has_page: bool = False,
    specific: bool = True,
) -> str:
    """The system prompt for one campaign's argument.

    Assembled rather than stored twice: the preamble and the rules are the
    same whichever argument is being made, and two full copies would drift
    the moment one was edited.

    ``pitch`` is what the message is actually offering -- "site" (the page,
    which is every existing caller) or "visual" (the attached picture). It is
    separate from ``angle`` because the angle decides which *argument* the
    campaign makes and the pitch decides which *artefact* carries it; a
    conversion campaign can pitch either.

    ``has_page`` and ``specific`` apply to the visual pitch only. The first
    adds the page link to the offer so "both tracks" stays one message; the
    second, when false, forbids describing a generic graphic as if it had
    been made for this business.
    """
    if pitch == "visual":
        beats = _BEATS_VISUAL
    elif angle == "conversion":
        beats = _BEATS_CONVERSION
    else:
        beats = _BEATS_NO_WEBSITE
    follow_up = _FOLLOW_UP_LINE if offers_follow_up else _NO_FOLLOW_UP_LINE
    extra = ""
    if pitch == "visual":
        if has_page:
            extra += _VISUAL_PLUS_PAGE
        if not specific:
            extra += _VISUAL_GENERIC_LINE
    return f"{_COPY_PREAMBLE}{beats}\n{_COPY_RULES}{follow_up}{extra}"


# The original argument, kept as a name because modelcheck and older callers
# ask for "the" system prompt without knowing about angles.
OUTREACH_COPY_SYSTEM = outreach_copy_system()



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
    layout_system: str = ""
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


@dataclass(frozen=True)
class TargetProfile:
    """What differs between a Lovable prompt and a Gemini-HTML prompt.

    Everything else in ``render_lovable_prompt`` -- the visitor, the copy,
    the section order, the motion *intent* -- is identical between targets;
    only the implementation vocabulary and a handful of platform-forced
    constraints change. Keeping those differences as data here, rather than
    ``if target == ...`` scattered through the function, is what makes a
    third target later a new dict entry instead of a re-edit of the whole
    prompt.
    """

    stack: str
    scroll_reveal: str
    reduced_motion: str
    # How this builder is told to declare its design tokens. The rule ("declare
    # them once, build from them") is the same everywhere; only the mechanism
    # differs, so the mechanism is the part that lives here.
    tokens_line: str
    enquiry_lines: tuple[str, ...]
    # Rendered inside STUDY THESE FIRST, only when there is a source to pull
    # from. Empty for html: a one-shot generation call cannot browse or
    # download a photo even where doing so were allowed, so there is nothing
    # useful to tell it to go look at.
    imagery_lines: tuple[str, ...]
    # Rendered unconditionally, sources or not -- html may never embed a
    # third-party photograph, regardless of what was found to look at. Empty
    # for lovable, whose IMAGERY block already covers this case.
    no_photography_lines: tuple[str, ...]
    # Platform-enforced DO NOT items, independent of anything the spec's
    # `avoid` field said about this specific business.
    fixed_do_not_lines: tuple[str, ...] = ()
    # Rendered verbatim by this function, not authored by the model -- a
    # fixed disclaimer is what turns a legal complaint into a reply-and-done
    # email rather than a "your AI put my name on a website" one. Empty for
    # lovable, which ships under the owner's own domain once approved.
    footer_line: str = ""
    output_format_lines: tuple[str, ...] = ()
    # The showcase craft block: typography as a scale, layout that breaks the
    # centred column, colour ratio, and motion stated in numbers. Rendered
    # only when design_level is "showcase". Craft rules, so template text --
    # every business gets the same answer and none of it earns a schema field.
    craft_lines: tuple[str, ...] = ()
    # One line about not shipping the component library's default look. What
    # that means differs enough per builder to live here.
    component_line: str = ""


# The showcase craft block. Shared between targets: type, layout, colour and
# timing are design, not framework, and the two builders differ only in the
# vocabulary for stating a duration. Written as fixed template text because
# every business gets the same answer -- see the note above the DESIGN SYSTEM
# block for why that matters.
_CRAFT_TYPE = (
    "TYPE AS A SYSTEM (the largest single difference between a designed page"
    " and a generated one)",
    "- Pick one scale and stay on it -- about 1.25 between steps on mobile,"
    " 1.33 from the large breakpoint up. Every size on the page is a step on"
    " that scale. A one-off font-size is the system leaking.",
    "- Leading tightens as size grows: body around 1.6, sub-heads about 1.3,"
    " the hero headline 1.0 to 1.1. Display type set at body leading is the"
    " most common reason a large headline looks amateur.",
    "- Cap the measure at 65-75 characters. A paragraph running the full width"
    " of a desktop container is hard to read whatever the typeface.",
    "- Tighten tracking on display sizes (around -0.02em), leave body alone,"
    " and open it slightly on any all-caps label.",
    "- Anything that sits in a column or changes in place -- prices, hours, a"
    " running total -- uses tabular figures, so the digits stop jittering.",
    "- Two families at most. Weight and size carry the hierarchy; a third"
    " typeface is decoration.",
)

_CRAFT_LAYOUT = (
    "LAYOUT (break the centred column -- a stack of identical centred boxes is"
    " exactly what a generated page looks like)",
    "- Not every section shares one container width. Let at least one run"
    " full-bleed edge to edge, and at least one sit narrower than the rest."
    " That change in width is most of what gives a page rhythm.",
    "- Vary vertical density on purpose. A dense proof strip against a"
    " generous hero reads as designed; the same padding on every section"
    " reads as a template.",
    "- Let one thing break the grid -- an image crossing a section boundary, a"
    " heading starting left of its column. One deliberate asymmetry per page"
    " is plenty, and it is the part people remember.",
    "- Line up what sits side by side. Two adjacent cards whose text does not"
    " share a baseline is the detail that reads as sloppy without anyone"
    " being able to say why.",
)

_CRAFT_COLOUR = (
    "COLOUR DISCIPLINE",
    "- Roughly 60 / 30 / 10: ground, secondary surface, accent. The accent is"
    " the smallest quantity on the page, and it is what the eye follows.",
    "- The accent belongs to the primary action. Spend it on headings, borders,"
    " icons and badges as well and the primary action stops standing out --"
    " which loses the one job the page has.",
    "- Separate surfaces with a tint or a hairline border before reaching for a"
    " shadow. A shadow on everything is the quickest way to look generic.",
    "- Check contrast at the sizes actually used: 4.5:1 for body, 3:1 for large"
    " text, and 3:1 for the focus ring against the surface it sits on rather"
    " than against the page.",
)


def _craft_motion(timing: str, easing: str) -> tuple[str, ...]:
    return (
        "MOTION, IN NUMBERS (a motion language given as an adjective becomes a"
        " different guess in every component)",
        "- Micro-interactions 120-200ms. Entrances and reveals 400-700ms."
        " Past about 800ms a thing reads as broken rather than slow.",
        f"- {easing}",
        "- Stagger siblings 60-80ms: enough to read as a sequence, not enough"
        " to leave the last card arriving late.",
        "- Move a short distance, 8-24px. A card sliding half the viewport is"
        " distracting, not expensive-looking.",
        "- One thing moves at a time in any one area of the screen. Two"
        " animations competing for the same attention cancel out.",
        f"- {timing}",
    )


# How much art direction the prompt carries.
#
# "standard" is what shipped before this switch existed. "showcase" adds the
# craft blocks -- type as a scale, layout that breaks the centred column,
# colour ratio, motion stated in numbers. Both produce the same *spec*: this
# only changes the prompt handed to the site builder, so no research model
# ever sees the extra text and a weaker one is not penalised for it.
DESIGN_LEVELS = ("standard", "showcase")

TARGETS: dict[str, TargetProfile] = {
    "lovable": TargetProfile(
        stack=(
            "Stack: React + Tailwind + shadcn/ui. Framer Motion is already available"
            " -- use motion.* directly. whileInView with viewport={{ once: true }}"
            " for on-scroll reveals, AnimatePresence for the showcase modal,"
            " layoutId for the card-to-modal shared-element move,"
            " useReducedMotion() as the a11y fallback. Spring for a snappy"
            " motion_language, tween for a slow one. Reach for shadcn's Dialog,"
            " Sheet, Tabs and Accordion over hand-rolled equivalents -- they"
            " already handle focus and escape."
        ),
        scroll_reveal=(
            "- Reveal on scroll with whileInView, viewport={{ once: true }};"
            " never re-trigger. Content is in the DOM and readable even if the animation never runs."
        ),
        reduced_motion=(
            "- Honour prefers-reduced-motion via useReducedMotion(): plain fade,"
            " or nothing. Some people get motion sickness from parallax."
        ),
        tokens_line=(
            "- Put the palette, spacing scale, radius and type steps into the"
            " Tailwind theme (tailwind.config, extend) and use those names in"
            " the markup -- bg-surface, text-muted, rounded-card. Arbitrary"
            " values like p-[13px] or text-[#3a2b1f] scattered through the JSX"
            " are the design system leaking."
        ),
        craft_lines=_CRAFT_TYPE + ("",) + _CRAFT_LAYOUT + ("",) + _CRAFT_COLOUR
        + ("",) + _craft_motion(
            timing="Keep durations and easings in the Tailwind theme too, and"
            " reuse them. A transition timed by hand in each component is the"
            " same leak as a hand-picked colour.",
            easing="Do not ship the default ease-in-out. Entrances ease out"
            " (quick, then settling); exits ease in. In Framer Motion that is"
            " a spring with a little overshoot for a snappy motion_language,"
            " or a tween on a custom cubic-bezier for a slow one -- which is"
            " what \"weighty\" or \"snappy\" actually means in practice.",
        ),
        component_line=(
            "- shadcn's defaults are recognisable on sight. Override the base"
            " before building on it -- radius, border, ring, shadow and font"
            " in the theme -- so a Dialog and a Button read as this business"
            " rather than as the default theme every shadcn site ships with."
        ),
        enquiry_lines=(
            "- Send every booking or enquiry to a spreadsheet via the",
            "  Google Sheets connector, one row per enquiry with a timestamp.",
            "  This owner has never had a website and will not log into an",
            "  admin panel to check messages. A spreadsheet is a tool they already know.",
            "- Put a WhatsApp click-to-chat link next to any form. In this market that is",
            "  how people actually get in touch, and a form alone will lose enquiries.",
        ),
        imagery_lines=(
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
        ),
        no_photography_lines=(),
    ),
    "html": TargetProfile(
        stack=(
            "Stack: one self-contained HTML file -- inline <style> and <script>,"
            " no build step, no framework, no external JS beyond the Google"
            " Fonts <link> tags. CSS @keyframes and transitions for motion,"
            " IntersectionObserver for on-scroll reveals, the native <dialog>"
            " element for the showcase modal (open it with .showModal(); it"
            " already handles focus and Escape for you), and a"
            " prefers-reduced-motion media query as the a11y fallback -- there"
            " is no framework hook to call for it here."
        ),
        scroll_reveal=(
            "- Reveal on scroll with IntersectionObserver, and unobserve each"
            " element after its first intersection so it never re-triggers."
            " Content is in the DOM and readable even if the observer callback never runs."
        ),
        reduced_motion=(
            "- Honour prefers-reduced-motion via a @media (prefers-reduced-motion:"
            " reduce) block: plain fade, or nothing. Some people get motion"
            " sickness from parallax."
        ),
        tokens_line=(
            "- Declare every token as a CSS custom property in a single :root"
            " block at the top of the <style> -- --bg, --surface, --text,"
            " --muted, --accent, --accent-hover, --space-1 through --space-6,"
            " --radius, --step-0 up the scale -- and use var() everywhere"
            " after it. A hex code or a raw px value further down the"
            " stylesheet is a token you forgot to name."
        ),
        craft_lines=_CRAFT_TYPE + ("",) + _CRAFT_LAYOUT + ("",) + _CRAFT_COLOUR
        + ("",) + _craft_motion(
            timing="Name the durations and easings as custom properties in the"
            " same :root block as the colours -- --dur-fast, --dur-reveal,"
            " --ease-out -- and use them everywhere. A duration typed by hand"
            " into one transition is the same leak as a raw hex code.",
            easing="Do not ship the browser default. Entrances ease out"
            " (quick, then settling); exits ease in. cubic-bezier(.16,1,.3,1)"
            " is a good out-curve and cubic-bezier(.7,0,.84,0) a good in-curve"
            " -- ease-in-out on everything is what makes motion feel generic.",
        ),
        component_line=(
            "- There is no component library here, so the risk is the opposite"
            " one: shipping the browser's default look. Style every control"
            " you use, including <dialog> and its ::backdrop, native selects"
            " and date inputs, and the focus ring -- an unstyled form control"
            " next to a designed page is what gives it away."
        ),
        enquiry_lines=(
            "- No backend exists for this site. Every enquiry must go through a",
            "  WhatsApp click-to-chat link, a tel: link, or a mailto: link --",
            "  nothing is there to receive a POST.",
            "- Never build a <form> that appears to submit. A form with nowhere",
            "  to send its data silently drops every enquiry the owner would",
            "  otherwise have received, which is worse than no form at all.",
            "- Put a WhatsApp click-to-chat link next to every contact point. In",
            "  this market that is how people actually get in touch.",
        ),
        imagery_lines=(),
        no_photography_lines=(
            "NO PHOTOGRAPHY",
            "No third-party photograph may appear anywhere on this site. "
            "Google's Places terms forbid caching a photo URI or file, and a "
            "single one-shot generation call cannot browse or download one "
            "even where that were allowed -- so any <img src> written here "
            "would be guessed, look plausible the moment it is generated, and "
            "404 the moment a real prospect clicks it.",
            "",
            "Design type-and-colour led instead, from the palette, typography, "
            "visual_direction and signature_moment above -- those are real "
            "design decisions, not a placeholder for missing photography. "
            "Every image slot becomes a solid tinted block from the palette "
            "with the item's name shown on it, the same honest fallback used "
            "wherever no photo source exists at all; never fill a gap with an "
            "invented photo.",
            "",
            "The one exception: a live Google Maps Embed of the actual "
            "premises (https://www.google.com/maps/embed/v1/place?"
            "q=place_id:THE_PLACE_ID -- fill in the real place ID from the "
            "Google Business Profile link above) is free-tier, permitted by "
            "Google's terms, and the one real visual allowed on the page.",
        ),
        fixed_do_not_lines=(
            "- No <img> or <script> whose src points at any domain other than",
            "  fonts.googleapis.com, fonts.gstatic.com, or www.google.com.",
            "- No <form> with an action attribute -- there is nowhere for it to",
            "  submit. Use a mailto:, tel:, or WhatsApp link instead.",
        ),
        footer_line=(
            "Unofficial concept by COAI -- not affiliated with {business_name}. "
            "Reply to have it removed."
        ),
        output_format_lines=(
            "",
            "OUTPUT FORMAT",
            "Return only the HTML document itself: <!doctype html> must be the "
            "first character of your reply and </html> the last character. No "
            "markdown code fences, no explanation before or after, nothing else "
            "in the response.",
            "Include <meta name=\"robots\" content=\"noindex\"> in <head> -- this "
            "is an unofficial concept site, not the business's own indexed page.",
        ),
    ),
}


def _enquiry_lines_for(profile: "TargetProfile", conversion: bool) -> tuple[str, ...]:
    """The shared enquiry instructions, with a reason true of this campaign.

    One line explains itself with "this owner has never had a website", which
    is false of a conversion lead -- they have one, that is the entire premise
    of the angle. The instruction it justifies is right either way: a small
    business owner will not log into an admin panel to read messages.

    Corrected here, at the point of use, rather than in the profile: the
    no-website prompt must come out byte for byte as it did before angles
    existed, which two snapshot tests hold. Telling the model to disregard a
    line it has just been given was the other option, and a prompt that
    argues with itself is not one to trust with a real business's page.
    """
    if not conversion:
        return profile.enquiry_lines
    return tuple(
        line.replace(
            "This owner has never had a website and will not log into an",
            "This owner runs a shop, not a website, and will not log into an",
        )
        for line in profile.enquiry_lines
    )


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
    target: str = "lovable",
    images: str = "",
    design_level: str = "standard",
    angle: str = "no-website",
) -> str:
    """Turn the structured spec into the text you paste into Lovable.

    Deterministic on purpose: the model decides *what* the site should be, this
    function decides how that is worded. Same spec in, same prompt out, which
    makes it reviewable, diffable, and testable -- and means a bad site is
    traceable to either the spec or this template, never to a coin flip.

    Structured the way a designer hands work over: who it's for, the one job,
    the copy, the art direction, what to leave out, then the build rules.

    ``target`` selects which builder this prompt is written for -- "lovable"
    (the default, and today's only behaviour) or "html", the self-contained
    single-file page a Gemini call renders straight to Firebase. The name
    stays ``render_lovable_prompt`` regardless: it is imported by
    ``site_brief.py`` and asserted by name across the tests, and the "html"
    target still IS a Lovable-shaped brief -- who it's for, the one job, the
    art direction -- just aimed at a different builder.
    """
    if target not in TARGETS:
        raise ValueError(
            f"Unknown build target {target!r}; expected one of {sorted(TARGETS)}"
        )
    if design_level not in DESIGN_LEVELS:
        raise ValueError(
            f"Unknown design level {design_level!r}; expected one of "
            f"{sorted(DESIGN_LEVELS)}"
        )
    profile = TARGETS[target]
    showcase = design_level == "showcase"
    conversion = angle == "conversion"

    where = f" in {location}" if location else ""
    # The conversion angle promises one page built for one action, and the
    # page is what they click -- so the spec's page list is overridden rather
    # than trusted. A model handed the same schema will happily propose five
    # sections, and a five-section site arriving under a message that
    # promised one page is the pitch contradicting itself at the only moment
    # anyone is looking.
    pages = (
        "- One page. Nothing else. No navigation bar, no About page, no links "
        "away from it."
        if conversion
        else "\n".join(f"- {p}" for p in spec.pages) or "- Home (single page)"
    )
    sections = "\n".join(f"{i}. {s}" for i, s in enumerate(spec.key_sections, start=1))

    if conversion:
        out = [
            f"Build a single landing page for {business_name}, a "
            f"{category or 'local business'}{where}.",
            "",
            "This business already has a website. This page is not a "
            "replacement for it and must never look like a redesign of it -- "
            "it sits alongside, for the visitors their current site is losing. "
            "Its whole purpose is that someone who is already interested has "
            "one obvious thing to do instead of leaving. The owner will see it "
            "before their customers do, and they paid for the site it sits "
            "beside, so nothing here may read as a criticism of what they have.",
        ]
    else:
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
        ]
        if profile.imagery_lines:
            out += ["", *profile.imagery_lines]

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
    ]

    if conversion:
        # The difference between a landing page and a short website is what
        # is missing from it. Said as removals, because a model asked for
        # "a focused page" still reaches for a nav bar and an about section.
        out += [
            "",
            "WHAT MAKES THIS A LANDING PAGE AND NOT A SMALL WEBSITE",
            f'- The way to get in touch -- "{spec.primary_cta}" -- is visible '
            "without scrolling, on a phone, and repeats at the end. Someone "
            "who arrives ready should never have to hunt for it.",
            "- No navigation bar and no menu. There is nowhere else to go, and "
            "every link off this page is a visitor who did not get in touch.",
            "- No About page, no blog, no gallery of everything they have ever "
            "done. Proof belongs on this page, in one short strip, only where "
            "it makes the next click more likely.",
            "- One action, not three. No 'call us OR email OR visit OR follow "
            "us' -- offering four choices is how a page gets none of them.",
            "- Short enough to read on a phone in under a minute. If a section "
            "does not make someone more likely to act, cut it rather than "
            "shorten it.",
        ]

    out += [
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
        # photo. On the html target IMAGERY never renders at all (see
        # NO PHOTOGRAPHY above), so the tinted-block wording is what every
        # card gets regardless of whether a source was found.
        if sources and profile.imagery_lines:
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
    # Structure before surface: the layout is what makes two sites look
    # different from each other, and a builder that reads the palette first
    # tends to recolour a default column rather than build the grid asked for.
    if spec.layout_system:
        out.append(f"Layout:  {spec.layout_system}")
    if spec.palette:
        out.append(f"Palette: {spec.palette}")
    if spec.typography:
        out.append(f"Type:    {spec.typography}")
    if spec.visual_direction:
        out.append(f"Feel:    {spec.visual_direction}")

    # Fixed craft rules, identical for every business, so they are template
    # text rather than schema fields -- the model has no per-lead decision to
    # make here and asking it to restate them each time only spends tokens on
    # a chance to get them wrong.
    out += [
        "",
        "DESIGN SYSTEM (declare once, build everything from it)",
        profile.tokens_line,
        "- One shadow level for the whole page, or none. A component that wants"
        " a second one is really asking for a border.",
        "- One card, one badge, one button, reused. Twelve slightly different"
        " cards is what a generated page looks like; one card used twelve"
        " times is what a designed one looks like.",
    ]
    if showcase and profile.component_line:
        out.append(profile.component_line)
    if showcase and profile.craft_lines:
        out += ["", *profile.craft_lines]
    out += [
        "",
        "INTERACTION STATES (the cheapest quality there is -- elements with one"
        " state each are most of why a page feels dead)",
        "- Every link, button and input: default, hover, focus-visible,"
        " pressed, and disabled or error where it applies. Mark the current"
        " nav item.",
        "- focus-visible is a real visible ring in the accent colour. Never"
        " `outline: none` without replacing it -- that is the usual way a site"
        " becomes unusable by keyboard.",
        "- Anything that waits has a waiting state, so a slow connection reads"
        " as busy rather than broken.",
    ]

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
        profile.stack,
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
        profile.scroll_reveal,
        profile.reduced_motion,
        "- Watch paint cost, not just layout: stacked large-radius shadows,"
        " full-page gradients and heavy blurs repaint every frame and turn a"
        " smooth animation into a stuttering one on the mid-range Android this"
        " is really for.",
        "- No scroll-jacking, no page-load animation gate, no cursor effects,"
        " no WebGL or Three.js. Those belong on a designer's portfolio, not a"
        " shop that needs the booking -- and the cursor ones do not exist on"
        " a phone.",
    ]

    # Design-decision constraint, not a business-specific one, so it does not
    # belong in `avoid` below and does not depend on whether the spec named
    # anything to avoid. Empty for lovable, whose IMAGERY block above already
    # covers this case with a real photo pipeline.
    if images.strip():
        # Real files, already generated and sitting next to index.html. They
        # are mood and texture -- never a depiction of this business's actual
        # premises, which is why the alt text describes what the image *is*
        # rather than claiming it shows their shop.
        out += [
            "",
            "IMAGES YOU HAVE (real files, in the same folder as index.html)",
            images.strip(),
            "",
            "Reference them exactly as written, as relative paths -- src=\"image-1.png\", "
            "not a URL. Use each one for the purpose given, set width and height so "
            "nothing jumps as they load, and lazy-load any below the fold. Use the alt "
            "text given: these are atmosphere, not photographs of this business, and "
            "must never be captioned as their premises, their staff or their work. "
            "Do not invent an image file that is not on this list, and do not link to "
            "an image anywhere else on the internet.",
        ]
    elif profile.no_photography_lines:
        out += ["", *profile.no_photography_lines]

    if spec.avoid:
        out += [
            "",
            "DO NOT (specific to this business)",
            *(f"- {a}" for a in spec.avoid),
        ]

    if profile.fixed_do_not_lines:
        out += [
            "",
            "DO NOT (platform rules -- apply regardless of the business)",
            *profile.fixed_do_not_lines,
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
            "the last line of content -- pad the page bottom by its height. "
            "Add env(safe-area-inset-bottom) to the bar's own bottom padding: "
            "without it the bar sits under the home indicator on every recent "
            "iPhone, which is exactly where the thumb already is.",
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
        "- Icons are inline SVG on currentColor at one square size -- no icon",
        "  font, no icon library, no emoji standing in for an icon. Draw the",
        "  simple graphics (rules, badges, section shapes) in CSS, not images.",
        "- Assume the text runs longer than it looks here: names and badges must",
        "  survive a second line without the card jumping or the button clipping.",
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
        *_enquiry_lines_for(profile, conversion),
    ]

    if profile.footer_line:
        out += [
            "",
            "FOOTER (render this exact text, verbatim, in a <footer> at the "
            "bottom of the page -- do not paraphrase, translate, or omit it)",
            profile.footer_line.format(business_name=business_name),
        ]

    if profile.output_format_lines:
        out += list(profile.output_format_lines)

    return "\n".join(out) + "\n"


GAP_WORDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "problem": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["id", "problem", "fix"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cards"],
    "additionalProperties": False,
}


GAP_WORDING_SYSTEM = """\
You re-word a fixed set of cards into one business's own vocabulary. You are \
not choosing what the cards say.

Each card has an id, a problem line and a solution line, already written. Your \
job is to say the same thing the way this trade says it -- "term fees" for a \
tuition centre where the card says "prices", "walk-ins" for a salon where it \
says "customers". Nothing else.

Hard rules:
- Return exactly the cards you were given, with exactly the same ids, in the \
same order. Never add a card. Never drop one. If you cannot improve a card, \
return its lines unchanged -- that is a correct answer, not a failure.
- The problem line is about what a customer could not do. It is NEVER about \
what the owner did wrong, did not bother to do, or got badly. This graphic \
goes to the owner with the sender's name under it, so a line that reads as \
criticism is the reason they stop reading. "Customers complain the staff are \
rude" is forbidden; "First-timers are not sure what to expect" says the same \
gap and can be acted on.
- Never name, quote or paraphrase a complaint, a bad review, or a star \
rating. The evidence is how the card was chosen; it is not the card.
- Plain words. No jargon, no marketing language, no superlatives, no \
exclamation marks, no emoji.
- These lines are set in a card on a graphic: at most 6 words for the problem \
and at most 7 for the solution. A long line is cropped by the image and \
becomes unreadable, which is worse than a plainer short one.
- Invent no fact about the business. Only the brief is evidence. If a card's \
wording would need a detail you were not given, leave that card as it is.
"""


@dataclass(frozen=True)
class GapCard:
    """One re-worded card, matched back to its rule by ``id``."""

    id: str
    problem: str
    fix: str


def build_gap_wording_prompt(
    *,
    name: str,
    category: str,
    location: str,
    tone: str,
    services: str,
    cards: Sequence[tuple[str, str, str]],
) -> str:
    """The evidence block for the card re-wording call.

    ``cards`` is (id, problem, fix) per card, already chosen. Deliberately
    narrow: the model is shown what this business sells and how it talks, and
    nothing that would tempt it into inventing a fourth gap. The pain points
    behind a card are NOT included -- a model handed the complaint will put
    the complaint on the card, which is the one thing the system prompt
    forbids.
    """
    lines = [
        f"Business: {name}",
        f"Category: {category or 'unknown'}",
        f"Location: {location or 'unknown'}",
        f"How they talk: {tone or 'unknown'}",
        f"What they sell: {services or 'unknown'}",
        "",
        "Cards to re-word (same ids, same order, same meaning):",
    ]
    for card_id, problem, fix in cards:
        lines.append(f'- id: {card_id}')
        lines.append(f'    problem: "{problem}"')
        lines.append(f'    fix: "{fix}"')
    return "\n".join(lines)


# The headline. Fixed text, because it is the same sentence for every business
# and the card count is the only thing in it that moves.
def _infographic_headline(business_name: str, count: int, specific: bool) -> str:
    wins = "quick win" if count == 1 else "quick wins"
    if specific and business_name:
        return f"{count} {wins} for {business_name}"
    return f"{count} {wins} for your business"


def render_infographic_prompt(
    *,
    business_name: str,
    cards: Sequence[tuple[str, str]],
    style: str,
    layout: str,
    character: str,
    palette: str,
    signature: str = "",
    aspect: str = "Wide 16:9 landscape",
    specific: bool = True,
) -> str:
    """The prompt Lyon pastes into Google Flow to generate one graphic.

    Deterministic, exactly like ``render_lovable_prompt``: the evidence
    decides what the cards say, this function decides how the request is
    worded. Same inputs in, same prompt out -- which is what makes a bad
    graphic traceable to either the evidence or this template, never to a
    coin flip.

    ``cards`` is (problem, fix) per row, already chosen and already re-worded.
    Its length drives the headline: two evidenced gaps produce a two-card
    graphic headed "2 quick wins", because three cards is the format and not
    a quota -- padding it is exactly the invention the whole track guards
    against.

    ``specific`` false renders the generic graphic: no business name anywhere,
    which is the honest shape for gaps the research could not evidence.

    The coral problem card and the mint solution card keep their colours
    whatever the palette says, and the signature ribbon keeps its place. A
    series that varies everything stops reading as one person's work.
    """
    if not cards:
        raise ValueError("render_infographic_prompt needs at least one card")

    name = business_name.strip()
    headline = _infographic_headline(name, len(cards), specific and bool(name))
    subject = f'"{name}"' if (specific and name) else "a small local business"

    out = [
        f"{aspect} semi-infographic, clean and simple, for {subject}. "
        f"Style: {style}. Palette: {palette}. Lots of white space, easy to "
        f"read at a glance on a phone.",
        "",
        f'Top headline, large: "{headline}". Beside the headline, '
        f"{character}, waving.",
        "",
        # The count leads, because the layout phrases describe a shape rather
        # than a number: a two-card graphic under a layout that said "three
        # stops" is the prompt contradicting itself, and the model resolves
        # that by inventing a third card.
        f"Layout: {len(cards)} numbered "
        f"{'step' if len(cards) == 1 else 'steps'}, arranged as {layout}. "
        f"Each card carries one simple cartoon icon.",
    ]
    for index, (problem, fix) in enumerate(cards, start=1):
        out.append(f'{index}: "{problem}" -> "{fix}"')

    if signature.strip():
        out += ["", f'Bottom ribbon in small text: "{signature.strip()}".']

    out += [
        "",
        # The colour rule survives every style. Stated after the palette on
        # purpose: a style phrase naming its own colours would otherwise
        # recolour the cards and break the series' one constant.
        "Keep the problem cards coral and the solution cards mint green in "
        "every case, whatever the palette above says -- those two colours are "
        "the same across this whole series.",
        # His own flag, and the one failure that cannot be fixed after
        # sending: a garbled word on a graphic with your name on it.
        "Text exactly as written above, correctly spelled, no extra words, no "
        "invented headings and no additional cards.",
        # A model asked for a business graphic will draw a logo for it. An
        # invented logo on a pitch to the business that owns the real one is
        # the same error as an invented price.
        "No logos of any kind: not this business's, not any other brand's, "
        "and do not invent one. No photographs of real people or real "
        "premises. No stars, no review quotes, no star ratings anywhere.",
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
    gaps: str = "",
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
    # What the attached picture actually says. The visual beats have always
    # told the model to "land on the first gap named in the Gaps block" --
    # and no such block was ever built, so every message was written blind to
    # the three statements it was supposed to be about. The id is stripped:
    # the model needs the claim, not the rule that chose it.
    if gaps:
        lines += ["", "What the attached picture says, in order:"]
        for index, line in enumerate(gaps.splitlines(), start=1):
            claim = line.split(":", 1)[-1].strip() if ":" in line else line.strip()
            if claim:
                lines.append(f"  {index}. {claim}")
        lines.append(
            "These are the points the picture makes. The hook lands on the "
            "first of them. Do not list them all -- they are already in the "
            "picture, and repeating them makes the message long and the "
            "picture pointless."
        )

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

    def _call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict:
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
            layout_system=str(payload.get("layout_system", "")).strip(),
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

    def suggest_areas(self, prompt: str) -> tuple[str, ...]:
        """Sub-areas for a broad location -- the wells discovery drinks."""
        payload = self._call(AREAS_SYSTEM, prompt, AREAS_SCHEMA)
        seen, out = set(), []
        for area in _as_tuple(payload.get("areas")):
            key = area.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(area.strip())
        return tuple(out)

    def write_gap_wording(
        self, prompt: str, *, timeout: float | None = None
    ) -> tuple[GapCard, ...]:
        """Re-word the chosen cards in this business's own vocabulary.

        Returns whatever the model gave back, unvalidated against the cards
        that went in: matching ids and dropping anything unrecognised is the
        caller's job, because the caller is the only thing holding the
        evidence each id has to stay attached to.
        """
        payload = self._call(
            GAP_WORDING_SYSTEM, prompt, GAP_WORDING_SCHEMA, timeout=timeout
        )
        out: list[GapCard] = []
        for card in payload.get("cards") or []:
            if not isinstance(card, dict):
                continue
            card_id = str(card.get("id", "")).strip()
            if not card_id:
                continue
            out.append(
                GapCard(
                    id=card_id,
                    problem=str(card.get("problem", "")).strip(),
                    fix=str(card.get("fix", "")).strip(),
                )
            )
        return tuple(out)

    def write_outreach_copy(
        self,
        prompt: str,
        *,
        angle: str = "no-website",
        offers_follow_up: bool = False,
        pitch: str = "site",
        has_page: bool = False,
        specific: bool = True,
    ) -> OutreachCopy:
        """Stage 5 -- the email and the WhatsApp opener.

        The angle decides which argument the message makes, so it has to
        arrive here rather than being read from a global: run and regenerate
        must produce the same message for the same campaign, and a default
        that silently applied to one of them is how three call sites once
        went on rendering the wrong prompt with every test still green.
        """
        payload = self._call(
            outreach_copy_system(
                angle,
                offers_follow_up=offers_follow_up,
                pitch=pitch,
                has_page=has_page,
                specific=specific,
            ),
            prompt,
            OUTREACH_COPY_SCHEMA,
        )
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

    def _call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict:
        self.call_count += 1
        try:
            extra = {} if timeout is None else {"timeout": timeout}
            response = self._client.messages.create(
                model=self.model,
                # Thinking is on by default on Opus 5 and counts against
                # max_tokens, so the budget below leaves room for both.
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=self._output_config(schema),
                **extra,
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

    def _post(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        timeout: float | None = None,
    ) -> str:
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
                url, json=body, headers=self._headers,
                timeout=self.timeout if timeout is None else timeout
            )
        except Exception as exc:  # noqa: BLE001 -- per-lead error, not a crash
            raise LLMError(f"Could not reach {url}: {type(exc).__name__}: {exc}") from exc

        if response.status_code != 200:
            raise LLMError(
                _explain_http(
                    response.status_code, response.text, url, self.model
                )
            )

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

    def _call(
        self,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict:
        self.call_count += 1
        messages = [
            {"role": "system", "content": system + "\n\n" + _json_instruction(schema)},
            {"role": "user", "content": prompt},
        ]

        text = self._post(messages, schema, timeout)
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
        return _validated(self._post(messages, schema, timeout), schema)


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


# A model that has been withdrawn says so in prose, and the status code it
# arrives under is not reliable -- Ollama answered 410, others answer 400 or
# 404 for the same fact. Matched on the provider's own sentence as well as on
# 410, because the difference between "retry later" and "this will never work
# again" is the whole of what the reader needs.
_RETIRED_MARKERS = (
    "was retired",
    "is retired",
    "has been retired",
    "no longer available",
    "no longer supported",
    "decommissioned",
    "has been removed",
)


def _provider_message(body: str) -> str:
    """The provider's own sentence, lifted out of whatever JSON wrapped it.

    An OpenAI-compatible error nests the one useful sentence under
    ``error.message`` and packs it in with ``type``, ``param``, ``code`` and a
    reference id -- a hundred characters of punctuation around the twenty that
    say what to do, printed raw on the dashboard where it reads as a crash.
    Same lesson as Google's ``SERVICE_DISABLED`` body, which repeated one
    sentence four times and buried the only link in the middle.
    """
    text = (body or "").strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text[:300]
    candidates = [payload]
    if isinstance(payload, dict):
        candidates.insert(0, payload.get("error"))
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
        if isinstance(value, dict):
            message = value.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:300]
    return text[:300]


def _explain_http(status: int, body: str, url: str, model: str = "") -> str:
    detail = _provider_message(body)
    lowered = detail.casefold()
    retired = status == 410 or any(mark in lowered for mark in _RETIRED_MARKERS)
    if retired:
        named = f"{model} " if model else "The model this campaign names "
        # The provider's sentence is relayed, never rewritten, and the
        # replacement it suggests is never copied into config by this code:
        # a model name has a shelf life and nothing in here can check one.
        # Asking the API is the rule; this is the API answering.
        return (
            f"{named}has been withdrawn by the provider, so nothing here can "
            "use it again. The provider says:\n\n"
            f"{detail}\n\n"
            "Open Settings, press Change beside the model, then press Show "
            "the models my key has -- that list comes from your own provider, "
            "so it is the only one that is true today. Pick one and press "
            "Test it. Running this again without changing it will fail in "
            "exactly the same way."
        )
    if status in (401, 403):
        return (
            f"{url} rejected the API key (HTTP {status}). Check the key in .env "
            "matches the provider in your campaign file -- OLLAMA_API_KEY for "
            "ollama-cloud, OPENROUTER_API_KEY for openrouter, GEMINI_API_KEY "
            "for gemini.\n" + detail
        )
    if status == 404:
        return (
            f"{url} returned 404. Usually the model name is wrong or not pulled "
            f"yet. For local Ollama try `ollama pull <model>` first.\n{detail}"
        )
    if status == 429:
        return f"Rate limited by {url} (HTTP 429).\n{detail}"
    return f"{url} returned HTTP {status}.\n{detail}"


def available_models(config: Any) -> list[str]:
    """Ask this campaign's provider which models its key can actually use.

    The oldest rule in this project is that no model name may be written into
    config from here, because a name correct today 404s in a month and nothing
    in this container can check one. The corollary nobody had built: the owner
    had no way to *ask* either. `outreach list-models` only ever spoke to
    Gemini, for `build.model`, and it is a terminal command besides -- so the
    morning a provider retired the model this campaign named, there was no
    answer available to him anywhere, at the moment every message had stopped
    being written. The guard that keeps a name out of this module is only
    half a policy without somewhere to get a real one.

    Every provider here but Anthropic is OpenAI-compatible and answers
    `GET /models`; Anthropic has its own path and its own header. Returned
    sorted, because a provider's own ordering is arbitrary and a list of
    forty names is read by eye.
    """
    provider = config.research.provider
    if provider == "anthropic":
        return _anthropic_models(config.secrets.anthropic_api_key)

    from ..config import PROVIDER_KEY_ENV

    key = config.secrets.llm_api_key
    if provider != "ollama" and not key:
        raise LLMError(
            f"research.provider is {provider!r} but "
            f"{PROVIDER_KEY_ENV.get(provider, 'the API key')} is not set in .env."
        )
    base_url = (config.research.base_url or "").rstrip("/")
    if not base_url:
        raise LLMError("No base_url for this provider -- set research.base_url.")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return _model_names(f"{base_url}/models", headers, "data")


def _anthropic_models(key: str) -> list[str]:
    """Through the SDK, which owns the version header and the pagination.

    A hand-rolled request here would hardcode `anthropic-version`, which is a
    second string with a shelf life in a function written because the first
    one expired.
    """
    if not key:
        raise LLMError("ANTHROPIC_API_KEY is not set in .env.")
    try:
        import anthropic
    except ImportError as exc:
        raise LLMError("The `anthropic` package is not installed") from exc
    try:
        page = anthropic.Anthropic(api_key=key).models.list(limit=100)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"{type(exc).__name__}: {exc}") from exc
    return sorted(
        {m.id for m in getattr(page, "data", []) if getattr(m, "id", "")}
    )


def _model_names(url: str, headers: dict[str, str], container: str) -> list[str]:
    """The names, out of whichever envelope this provider uses.

    Reads `id` and falls back to `name`: both spellings are in the wild, and a
    provider using the other one would otherwise return a list of blanks --
    which reads as "your key has no models" and sends someone to rotate a key
    that is perfectly good.
    """
    import requests

    try:
        response = requests.get(url, headers=headers, timeout=30)
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"Could not reach {url}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise LLMError(_explain_http(response.status_code, response.text, url))
    try:
        payload = response.json()
    except ValueError as exc:
        raise LLMError(f"{url} did not answer with JSON: {exc}") from exc

    rows = payload.get(container) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise LLMError(f"{url} answered in an unexpected shape.")
    names = set()
    for row in rows:
        if isinstance(row, str):
            names.add(row.strip())
        elif isinstance(row, dict):
            name = row.get("id") or row.get("name") or ""
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    return sorted(n for n in names if n)


def make_writer(config: Any, *, model: str | None = None) -> _Writer:
    """Build the writer this campaign asks for.

    One place decides, so every stage gets the same model without each of them
    knowing which provider is in play. Pass ``model`` to override the research
    model for one stage -- the outreach draft uses a smaller model when the
    campaign sets ``outreach.model``.
    """
    provider = config.research.provider
    model = model or config.research.model
    if provider == "anthropic":
        return BriefWriter(
            config.secrets.anthropic_api_key,
            model=model,
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
        key, base_url=config.research.base_url, model=model
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    return tuple(str(v).strip() for v in (value or []) if str(v).strip())
