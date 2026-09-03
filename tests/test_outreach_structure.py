"""The shape of a first-contact message, and who it is from.

Lyon asked for hook -> solution -> offer -> model. The model writes the
words, so what can be pinned here is that it is told to, in that order, and
that the rules which stop a sales-shaped message getting invented survive a
later reword.
"""

import re

from outreach.campaign_edit import CampaignEditError, rewrite_campaign
from outreach.config import load_config
from outreach.providers.llm import OUTREACH_COPY_SYSTEM

PENANG = "config/campaigns/penang-my.yaml"


class TestTheFourBeats:
    def test_all_four_are_named(self):
        for beat in ("HOOK", "SOLUTION", "OFFER", "MODEL"):
            assert beat in OUTREACH_COPY_SYSTEM, f"{beat} is not in the brief"

    def test_they_are_briefed_in_order(self):
        positions = [OUTREACH_COPY_SYSTEM.index(b) for b in ("HOOK", "SOLUTION", "OFFER", "MODEL")]
        assert positions == sorted(positions), "the beats are briefed out of order"

    def test_the_hook_must_be_specific_to_the_business(self):
        """A hook that would suit any business is the failure mode this
        structure exists to prevent, so the instruction has to say so."""
        assert "impossible to send to any other business" in OUTREACH_COPY_SYSTEM

    def test_the_model_paragraph_explains_what_happens_next(self):
        """Without it the message reads as a trick, and an owner who suspects
        a trick does not reply."""
        assert "what happens next" in OUTREACH_COPY_SYSTEM

    def test_nothing_commercial_may_be_invented(self):
        """The four-beat shape invites a price or a deadline that was never
        given. Those are the claims an owner can hold Lyon to."""
        assert "Never invent a price" in OUTREACH_COPY_SYSTEM

    def test_the_whatsapp_version_keeps_the_structure(self):
        assert "keeps all four beats" in OUTREACH_COPY_SYSTEM

    def test_the_ask_is_still_small(self):
        """Carried over from the previous brief: an offer paragraph makes it
        tempting to ask for a meeting."""
        assert "a reply, not a meeting" in OUTREACH_COPY_SYSTEM


class TestTheSenderName:
    def test_the_campaign_says_lyon_ming(self):
        assert load_config(PENANG, env_file=None).outreach.sender_name == "Lyon Ming"

    def test_the_name_can_be_changed_from_the_page(self):
        """The tracked file is not what runs once settings have been saved --
        the dashboard forks to a .local twin -- so a name that could only be
        changed by editing YAML would look like it had been ignored."""
        original = open(PENANG).read()
        updated = rewrite_campaign(original, sender_name="Someone Else")

        assert 'sender_name: "Someone Else"' in updated
        assert "Lyon Ming" not in updated

    def test_an_empty_name_is_refused(self):
        original = open(PENANG).read()
        try:
            rewrite_campaign(original, sender_name="   ")
        except CampaignEditError as exc:
            assert "cannot be empty" in str(exc)
        else:
            raise AssertionError("an empty sender name was accepted")

    def test_rewriting_the_name_leaves_the_rest_alone(self):
        """Comments and every other setting have to survive the edit."""
        original = open(PENANG).read()
        updated = rewrite_campaign(original, sender_name="Someone Else")

        assert updated.count("\n") == original.count("\n")
        assert "postal_address:" in updated
        assert re.search(r"^campaign:", updated, re.M)
