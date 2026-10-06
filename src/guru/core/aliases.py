"""In-memory alias matcher (token n-gram index). O(tokens × max_alias_len), no dependencies.

Rebuilt whenever config or reference data changes. Greek aliases are also indexed under their
Greeklish spellings so "pou pefti to x" finds the same entity as "πού πέφτει το x".
"""

from __future__ import annotations

from dataclasses import dataclass

from guru.core.text import greeklish_variants, normalize, tokens


@dataclass(frozen=True)
class AliasTarget:
    target_type: str  # 'entity' | 'category' | 'intent' | 'profile'
    target_key: str
    weight: float = 1.0


@dataclass(frozen=True)
class AliasHit:
    start: int  # token offsets in the query
    end: int
    alias: str
    target: AliasTarget


class AliasMatcher:
    def __init__(self) -> None:
        self._index: dict[str, list[tuple[tuple[str, ...], AliasTarget]]] = {}
        self._max_len = 1
        self.size = 0

    def add(self, alias: str, target: AliasTarget, *, with_greeklish: bool = True) -> None:
        forms = {normalize(alias)}
        if with_greeklish:
            forms |= greeklish_variants(alias)
        for form in forms:
            toks = tuple(form.split()) if form else ()
            if not toks:
                continue
            self._index.setdefault(toks[0], []).append((toks, target))
            self._max_len = max(self._max_len, len(toks))
            self.size += 1

    def match(self, text: str) -> list[AliasHit]:
        """Longest-match-first, non-overlapping hits in reading order."""
        toks = tokens(text)
        hits: list[AliasHit] = []
        i = 0
        while i < len(toks):
            best: tuple[int, tuple[str, ...], list[AliasTarget]] | None = None
            for cand, target in self._index.get(toks[i], ()):
                n = len(cand)
                if tuple(toks[i : i + n]) == cand and (best is None or n > best[0]):
                    best = (n, cand, [target])
                elif best is not None and n == best[0] and tuple(toks[i : i + n]) == cand:
                    best[2].append(target)
            if best is None:
                i += 1
                continue
            n, cand, targets = best
            hits.extend(AliasHit(i, i + n, " ".join(cand), t) for t in targets)
            i += n
        return hits
