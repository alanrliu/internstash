"""Tag normalization and canonicalization.

Tags arrive from two places -- the LLM extractor and the feed importer -- and
neither agrees with itself about wording. "swe", "software-engineering", and
"software-development" all mean the same thing to a person browsing a board.

Canonicalization happens at READ time only. The database keeps whatever text was
originally stored, so:
  * editing config.TAG_SYNONYMS re-groups existing entries retroactively, and
  * nothing the model produced is ever destroyed.
"""

import re
from collections import Counter
from typing import Iterable

from . import config

def normalize(tag: str) -> str:
    """Lowercase, trim, and collapse separators to single hyphens.

    `&`, `/`, `|` and `_` are treated as separators so the feed's
    "Data & ML/AI" category normalizes to "data-ml-ai". `+` and `.` are left
    alone -- otherwise "c++" and "node.js" would be mangled.
    """
    t = tag.strip().lower()
    for ch in "_/&|,":
        t = t.replace(ch, " ")
    t = t.replace("-", " ")
    return "-".join(t.split())


# Flattened {variant: canonical} lookup, built once at import. Both sides are
# normalized, so config.TAG_SYNONYMS can be written naturally ("Data & ML/AI")
# without worrying about matching the stored spelling exactly.
_VARIANT_TO_CANONICAL: dict[str, str] = {}
for _canonical, _variants in config.TAG_SYNONYMS.items():
    _key = normalize(_canonical)
    _VARIANT_TO_CANONICAL[_key] = _key
    for _v in _variants:
        _VARIANT_TO_CANONICAL[normalize(_v)] = _key

# Canonical "area" tags, as opposed to incidental skill tags. Areas sort first
# in the filter because they are what you actually browse by.
_AREAS = {normalize(k) for k in config.TAG_SYNONYMS}


def canonical(tag: str) -> str:
    """Map one tag to its canonical form, or return it normalized if unknown."""
    norm = normalize(tag)
    return _VARIANT_TO_CANONICAL.get(norm, norm)


# Word-boundary regex per known variant, for guessing tags out of free text
# (e.g. a bare job title) when a source gives us no explicit category/skills.
# Unlike FETCH_KEYWORDS' deliberate substring matching, a short variant like
# "ai" or "ml" must NOT fire on "main" or "html" -- so this matches whole
# words only, allowing the source text to use a space or hyphen wherever the
# variant itself uses a hyphen ("machine-learning" also matches "Machine
# Learning").
_VARIANT_PATTERNS: dict[str, re.Pattern] = {}
for _canonical, _variants in config.TAG_SYNONYMS.items():
    for _v in [_canonical] + _variants:
        _norm_v = normalize(_v)
        if not _norm_v:
            continue
        _pattern = re.escape(_norm_v).replace(r"\-", r"[\s-]+")
        _VARIANT_PATTERNS[_v] = re.compile(rf"\b{_pattern}\b", re.IGNORECASE)


def infer_from_text(text: str | None) -> list[str]:
    """Best-effort canonical tags guessed from free text.

    Used as a fallback when a source provides no explicit category/skills, so
    its cards aren't left with no tag chips at all. Conservative by design
    (word-boundary only) since this drives what's shown, not just triage.
    """
    if not text:
        return []
    found: set[str] = set()
    for variant, pattern in _VARIANT_PATTERNS.items():
        if pattern.search(text):
            found.add(canonical(variant))
    return sorted(found)


def split_tags(raw: str | None) -> list[str]:
    """Parse a stored comma-separated tag string into raw tags."""
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def canonical_set(raw: str | None) -> set[str]:
    """Canonical tags for one row, deduplicated."""
    return {canonical(t) for t in split_tags(raw) if canonical(t)}


def summarize(rows: Iterable[dict]) -> list[dict]:
    """Build the filter list: canonical tags with counts and their variants.

    Sorted by frequency so the tags you actually use float to the top.
    """
    counts: Counter[str] = Counter()
    variants: dict[str, set[str]] = {}

    for row in rows:
        seen = set()
        for raw in split_tags(row.get("tags")):
            c = canonical(raw)
            if not c:
                continue
            variants.setdefault(c, set()).add(normalize(raw))
            if c not in seen:      # count each row once per canonical tag
                counts[c] += 1
                seen.add(c)

    facets = [
        {
            "tag": tag,
            "count": n,
            "is_area": tag in _AREAS,
            # Only worth showing when something actually folded into it.
            "variants": sorted(v for v in variants.get(tag, set()) if v != tag),
        }
        for tag, n in counts.most_common()
    ]
    # Areas first (each by frequency), then skill tags by frequency.
    facets.sort(key=lambda f: (not f["is_area"], -f["count"], f["tag"]))
    return facets


def matches_any(raw: str | None, wanted: Iterable[str]) -> bool:
    """True if the row carries any of the requested canonical tags (OR)."""
    wanted_set = {canonical(w) for w in wanted}
    return bool(canonical_set(raw) & wanted_set)
