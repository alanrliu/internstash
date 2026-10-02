"""Season / term parsing, ordering, and faceting.

The upstream feed carries a `season` string ("Summer 2027", "Fall 2026", or
"Not stated"). We store it as its own column so it can be filtered on as a
distinct axis from tags -- a term is a scale, not a topic.

"Not stated" becomes NULL: it is a genuine third state (51 of 126 feed entries
at time of writing), not a parse failure.
"""

import re
from collections import Counter
from typing import Iterable, Optional

# "Summer 2027", "summer, 2027", "FALL 2026", "Autumn 2026"
_SEASON_RE = re.compile(
    r"\b(spring|summer|fall|autumn|winter)\b[\s,]*((?:19|20)\d{2})", re.IGNORECASE
)

# Within a year, terms run in this order.
_TERM_ORDER = {"spring": 0, "summer": 1, "fall": 2, "winter": 3}

# Autumn is the same thing as Fall; normalize so they don't split the facet.
_TERM_ALIASES = {"autumn": "fall"}

_NOT_STATED = {"not stated", "not specified", "unknown", "n/a", "none", ""}


def normalize(raw: Optional[str]) -> Optional[str]:
    """Return a canonical "Summer 2027", or None if there isn't a real term.

    Accepts free text, so it also works on a job title or description body.
    """
    if not raw:
        return None
    if raw.strip().lower() in _NOT_STATED:
        return None

    m = _SEASON_RE.search(raw)
    if not m:
        return None

    term = m.group(1).lower()
    term = _TERM_ALIASES.get(term, term)
    return f"{term.capitalize()} {m.group(2)}"


def sort_key(season: Optional[str]) -> tuple:
    """Chronological ordering: Fall 2026 before Spring 2027 before Summer 2027.

    Unknown sorts last.
    """
    if not season:
        return (9999, 9, "")
    m = _SEASON_RE.search(season)
    if not m:
        return (9999, 9, season)
    term = _TERM_ALIASES.get(m.group(1).lower(), m.group(1).lower())
    return (int(m.group(2)), _TERM_ORDER.get(term, 9), season)


def summarize(rows: Iterable[dict]) -> list[dict]:
    """Season facets with counts, in chronological order.

    Rows with no season are grouped under a single null entry so they remain
    reachable in the filter rather than silently vanishing.
    """
    counts: Counter[Optional[str]] = Counter()
    for row in rows:
        counts[normalize(row.get("season")) or None] += 1

    known = sorted((s for s in counts if s), key=sort_key)
    facets = [{"season": s, "count": counts[s]} for s in known]
    if counts.get(None):
        facets.append({"season": None, "count": counts[None], "label": "not stated"})
    return facets
