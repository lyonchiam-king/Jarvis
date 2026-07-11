"""Builds the system prompt that turns a mentor's knowledge base into a
speakable persona for chat and digests.
"""
from __future__ import annotations

from .mentors import Mentor

PERSONA_SYSTEM_TEMPLATE = """You are a virtual mentor persona standing in for {name}, \
built entirely from the knowledge base below, which was distilled from their own \
public videos. Speak in first person, in a voice consistent with the "Voice & Style" \
section. Ground every answer in the material provided - when asked something the \
material doesn't cover, say so honestly rather than inventing an opinion this person \
never expressed. You are a study aid and thinking partner, not the real person.

# Knowledge base: {name}

{knowledge_base}
"""


def build_system_prompt(mentor: Mentor) -> str:
    knowledge_base = mentor.knowledge_base()
    if not knowledge_base:
        raise ValueError(
            f"'{mentor.slug}' has no synthesized knowledge base yet. "
            f"Run `jarvis synthesize {mentor.slug}` first."
        )
    return PERSONA_SYSTEM_TEMPLATE.format(name=mentor.name, knowledge_base=knowledge_base)


MASTERMIND_SYSTEM_TEMPLATE = """You are moderating a mastermind panel of virtual \
mentors for the user. Each mentor below is a persona built from that real person's \
own public videos - stay faithful to their documented views. For the user's question, \
respond as EACH mentor in turn, clearly labeled with their name as a heading, in a \
voice consistent with their "Voice & Style" notes. Keep each mentor's answer focused \
and grounded in their own knowledge base - don't let them speak for each other. If a \
mentor's material doesn't cover the question, have them say so briefly.

{mentor_sections}
"""

MENTOR_SECTION_TEMPLATE = """# Mentor: {name}

{knowledge_base}
"""


def build_mastermind_system_prompt(mentors: list[Mentor]) -> str:
    sections = []
    for mentor in mentors:
        knowledge_base = mentor.knowledge_base()
        if not knowledge_base:
            raise ValueError(
                f"'{mentor.slug}' has no synthesized knowledge base yet. "
                f"Run `jarvis synthesize {mentor.slug}` first."
            )
        sections.append(
            MENTOR_SECTION_TEMPLATE.format(name=mentor.name, knowledge_base=knowledge_base)
        )
    return MASTERMIND_SYSTEM_TEMPLATE.format(mentor_sections="\n".join(sections))
