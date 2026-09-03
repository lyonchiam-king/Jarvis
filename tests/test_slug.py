import hashlib

from outreach.slug import site_slug


def _hash(lead_id: str) -> str:
    return hashlib.sha256(lead_id.encode()).hexdigest()[:4]


def test_a_readable_name_becomes_a_readable_slug():
    slug = site_slug("Sun Salon", "ChIJxyz")
    assert slug == f"sun-salon-{_hash('ChIJxyz')}"


def test_same_inputs_give_the_same_output_across_calls():
    a = site_slug("Sun Salon", "ChIJxyz")
    b = site_slug("Sun Salon", "ChIJxyz")
    assert a == b


def test_two_businesses_with_the_same_name_get_different_slugs():
    """Different Lead IDs -- the case a stranger trimming a link must not
    collide across two different businesses sharing a name."""
    a = site_slug("Sun Salon", "ChIJ-lead-one")
    b = site_slug("Sun Salon", "ChIJ-lead-two")
    assert a != b
    assert a.startswith("sun-salon-")
    assert b.startswith("sun-salon-")


def test_accents_fold_to_their_base_letters():
    slug = site_slug("Café Lumière", "lead-1")
    assert slug == f"cafe-lumiere-{_hash('lead-1')}"


def test_a_name_that_folds_to_nothing_yields_the_bare_hash():
    """All-CJK. This is the case that crashes a naive f'{name}-{hash}'."""
    slug = site_slug("髮型屋", "lead-cjk")
    assert slug == _hash("lead-cjk")
    assert not slug.startswith("-")


def test_a_name_of_only_punctuation_yields_the_bare_hash():
    slug = site_slug("!!! ---", "lead-punct")
    assert slug == _hash("lead-punct")


def test_a_very_long_name_is_trimmed_and_does_not_end_in_a_stray_dash():
    long_name = "The Best Hair Salon And Spa In All Of Georgetown Penang"
    slug = site_slug(long_name, "lead-long")
    name_part, _, digest = slug.rpartition("-")
    assert len(name_part) <= 40
    assert not name_part.endswith("-")
    assert digest == _hash("lead-long")


def test_the_hash_suffix_is_always_four_hex_characters():
    slug = site_slug("A", "lead-x")
    suffix = slug.rsplit("-", 1)[-1]
    assert len(suffix) == 4
    int(suffix, 16)  # raises if it is not hex


def test_empty_business_name_yields_the_bare_hash():
    slug = site_slug("", "lead-empty")
    assert slug == _hash("lead-empty")
