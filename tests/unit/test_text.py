from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from guru.core.aliases import AliasMatcher, AliasTarget
from guru.core.text import (
    content_tokens,
    detect_language,
    greeklish_variants,
    normalize,
    to_greeklish,
    tokens,
)


def test_normalize_accents_case_final_sigma() -> None:
    assert normalize("Πού ΠΈΦΤΕΙ το Σπαθί;") == "που πεφτει το σπαθι"
    assert normalize("Ο μονομάχος") == normalize("ο ΜΟΝΟΜΑΧΟΣ") == "ο μονομαχοσ"
    assert normalize("Café  CRÈME!!") == "cafe creme"


def test_tokens_keep_versions_and_contractions() -> None:
    assert tokens("patch v1.2.3 is live, don't miss") == ["patch", "v1.2.3", "is", "live", "don't", "miss"]


@given(st.text())
def test_normalize_idempotent(s: str) -> None:
    assert normalize(normalize(s)) == normalize(s)


def test_detect_language() -> None:
    assert detect_language("Πού βρίσκω τον boss;").style == "el"
    assert detect_language("pou vriskw ton boss?").style == "greeklish"
    assert detect_language("pws kanw enchant").style == "greeklish"
    assert detect_language("where does the boss spawn?").style == "en"
    assert detect_language("boss respawn").style == "en"


def test_content_tokens_drop_stopwords_all_languages() -> None:
    assert content_tokens("pou einai o boss?") == ["boss"]
    assert content_tokens("Πού είναι ο boss;") == ["boss"]
    assert content_tokens("where is the boss at level 45") == ["boss", "level", "45"]


def test_to_greeklish() -> None:
    assert to_greeklish("Ο μονομάχος είναι tank") == "O monomaxos einai tank"
    assert to_greeklish("Πού πέφτει;") == "Pou peftei;"


def test_greeklish_variants_cover_common_spellings() -> None:
    v = greeklish_variants("μονομάχος")
    assert {"monomaxos", "monomahos", "monomachos", "monomaxws"} & v == {"monomaxos", "monomahos", "monomachos"}
    assert "8ea" in greeklish_variants("θέα")
    assert greeklish_variants("boss") == set()


def test_alias_matcher_longest_match_and_greeklish() -> None:
    m = AliasMatcher()
    m.add("Fire Temple", AliasTarget("entity", "1"))
    m.add("Fire", AliasTarget("entity", "2"))
    m.add("μονομάχος", AliasTarget("entity", "3"))
    hits = m.match("Where is the fire temple boss?")
    assert [(h.alias, h.target.target_key) for h in hits] == [("fire temple", "1")]
    assert [h.target.target_key for h in m.match("fire dmg")] == ["2"]
    assert [h.target.target_key for h in m.match("ti build gia monomaxo; monomaxos")] == ["3"]
    assert [h.target.target_key for h in m.match("Ο ΜΟΝΟΜΆΧΟΣ")] == ["3"]


def test_alias_matcher_multiple_targets_same_alias() -> None:
    m = AliasMatcher()
    m.add("drop", AliasTarget("category", "items"))
    m.add("drop", AliasTarget("intent", "item_source"))
    assert {h.target.target_type for h in m.match("drop rate")} == {"category", "intent"}
