"""Deterministic text handling: normalization, script/language detection, Greeklish.

The same `normalize` is applied to stored text and to queries, so FTS and alias matching
never depend on accents, case or Unicode forms.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

_TOKEN_RE = re.compile(r"[^\W_]+(?:[.'’][^\W_]+)*", re.UNICODE)
_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_LATIN_RE = re.compile(r"[a-zA-Z]")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokens(text: str) -> list[str]:
    """Normalized tokens: accents stripped, casefolded (ς→σ), punctuation removed."""
    folded = strip_accents(text).casefold()
    return [t.replace("’", "'") for t in _TOKEN_RE.findall(folded)]


def normalize(text: str) -> str:
    return " ".join(tokens(text))


def has_number(text: str) -> bool:
    return _NUMBER_RE.search(text) is not None


# ---------------------------------------------------------------- script & language

Script = Literal["greek", "latin", "other"]
Style = Literal["en", "el", "greeklish"]


def detect_script(text: str) -> Script:
    greek = len(_GREEK_RE.findall(text))
    latin = len(_LATIN_RE.findall(text))
    if greek == 0 and latin == 0:
        return "other"
    return "greek" if greek >= latin else "latin"


# Frequent Greek function words as typically written in Greeklish (normalized forms).
GREEKLISH_MARKERS = frozenset(
    """
    kai ki einai ine itan pws pos pou poy poio poia poios poso posa posoi pote giati gt gia ya
    na tha den dn de mou moy sou soy mas sas tous tis twn ton tin thn tha me se apo ap sto sth
    stin stis ston sta afto auto ayto afta auta ayta auth aftos kati kapoios kanei kanw kano
    exei exw exo exeis eimai eisai mporw mporo mporei prepei thelw thelo kserei kserw ksero
    nai oxi ohi ola olo oloi kala kalo kalh kali polu poli ligo pio mono akoma akomh twra tora
    meta prin otan opou opws dld dhladh dhl re reee leme pame vazw bazw vriskw brisko vrisko
    """.split()
)

_GREEK_STOPWORDS = frozenset(
    tokens(
        """και κι είναι ήταν πως πώς που πού ποιο ποια ποιος πόσο πόσα πότε γιατί για να θα δεν μη μην
        μου σου μας σας τους τις των τον την το τα η ο οι με σε από στο στη στην στις στον στα
        αυτό αυτά αυτή αυτός κάτι κάνει έχει έχω είμαι μπορώ πρέπει θέλω ναι όχι όλα πολύ πιο
        μόνο ακόμα τώρα μετά πριν όταν όπου όπως δηλαδή ένα μια ένας"""
    )
)

_ENGLISH_STOPWORDS = frozenset(
    """
    a an the is are was were be been am do does did to of in on at for from by with and or not
    no yes it its this that these those what which who whom whose where when why how can could
    should would will shall may might must i you he she we they me him her us them my your our
    their there here any some all much many more most very just also than then so if about into
    get got has have had please tell know does anyone someone pls plz
    """.split()
)

STOPWORDS = _ENGLISH_STOPWORDS | _GREEK_STOPWORDS | GREEKLISH_MARKERS


@dataclass(frozen=True)
class LanguageGuess:
    lang: Literal["en", "el"]
    style: Style
    script: Script


def detect_language(text: str) -> LanguageGuess:
    """Cheap deterministic guess: Greek script → el; Latin with Greeklish markers → el/greeklish."""
    script = detect_script(text)
    if script == "greek":
        return LanguageGuess("el", "el", script)
    toks = tokens(text)
    if toks:
        hits = sum(1 for t in toks if t in GREEKLISH_MARKERS)
        english = sum(1 for t in toks if t in _ENGLISH_STOPWORDS)
        if (hits >= 2 and hits > english) or (hits >= 1 and len(toks) <= 4 and english == 0):
            return LanguageGuess("el", "greeklish", script)
    return LanguageGuess("en", "en", script)


def content_tokens(text: str) -> list[str]:
    """Tokens worth searching for: no stopwords (en/el/Greeklish), no 1-char noise."""
    return [t for t in tokens(text) if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


# ---------------------------------------------------------------- Greeklish

# Canonical transliteration (used to render answers in Greeklish).
_GREEKLISH_MAP: dict[str, str] = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "th", "ι": "i",
    "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "ks", "ο": "o", "π": "p", "ρ": "r", "σ": "s",
    "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "x", "ψ": "ps", "ω": "w",
}  # fmt: skip
_DIGRAPHS: dict[str, str] = {"ου": "ou", "αι": "ai", "ει": "ei", "οι": "oi", "μπ": "mp", "ντ": "nt", "γκ": "gk"}

# Spelling variants people actually use, per Greek letter/digraph (first = canonical).
_VARIANTS: dict[str, tuple[str, ...]] = {
    "η": ("i", "h"), "θ": ("th", "8"), "υ": ("y", "i", "u"), "χ": ("x", "h", "ch"),
    "ω": ("w", "o"), "ξ": ("ks", "x"), "ψ": ("ps",), "β": ("v", "b"), "ου": ("ou", "u"),
    "ει": ("ei", "i"), "οι": ("oi", "i"), "αι": ("ai", "e"), "μπ": ("mp", "b"), "ντ": ("nt", "d"),
    "γκ": ("gk", "g"), "φ": ("f", "ph"),
}  # fmt: skip


def _greek_units(word: str) -> list[str]:
    units, i = [], 0
    while i < len(word):
        pair = word[i : i + 2]
        if pair in _DIGRAPHS:
            units.append(pair)
            i += 2
        else:
            units.append(word[i])
            i += 1
    return units


def to_greeklish(text: str) -> str:
    """Deterministic Greek→Greeklish (keeps case of the first letter of each word)."""
    out: list[str] = []
    for word in re.split(r"(\s+)", text):
        if not _GREEK_RE.search(word):
            out.append(word)
            continue
        base = strip_accents(word)
        lower = base.lower()
        translit = "".join(_DIGRAPHS.get(u, _GREEKLISH_MAP.get(u, u)) for u in _greek_units(lower))
        if base[:1].isupper():
            translit = translit[:1].upper() + translit[1:]
        out.append(translit)
    return "".join(out)


def greeklish_variants(greek_text: str, limit: int = 24) -> set[str]:
    """Plausible Greeklish spellings of a (short) Greek alias, normalized, bounded in number."""
    norm = normalize(greek_text)
    if not _GREEK_RE.search(norm):
        return set()
    words = norm.split(" ")
    per_word: list[list[str]] = []
    for word in words:
        options = [""]
        for unit in _greek_units(word):
            choices = _VARIANTS.get(unit) or (_DIGRAPHS.get(unit) or _GREEKLISH_MAP.get(unit, unit),)
            options = [o + c for o in options for c in choices][:limit]
        per_word.append(options)
    combos = itertools.islice(itertools.product(*per_word), limit)
    return {" ".join(c) for c in combos}
