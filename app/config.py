"""Single source of truth for every path and tunable in InternStash.

If you are looking for "where does my data live" -- it is DATA_DIR, and nothing
user-specific is written anywhere else. Every value here can be overridden with
an INTERNSTASH_* environment variable so you never have to edit code to
relocate the database.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Paths -----------------------------------------------------------------
# Everything user-specific goes under DATA_DIR. data/ is gitignored; see
# data/README.md. Do not write user data outside this directory.
DATA_DIR = Path(
    os.environ.get("INTERNSTASH_DATA_DIR", PROJECT_ROOT / "data")
).resolve()

DB_PATH = Path(
    os.environ.get("INTERNSTASH_DB_PATH", DATA_DIR / "internstash.db")
).resolve()

LOG_PATH = Path(os.environ.get("INTERNSTASH_LOG_PATH", DATA_DIR / "internstash.log"))

STATIC_DIR = PROJECT_ROOT / "app" / "static"

# --- Server ----------------------------------------------------------------
# Binds to 0.0.0.0 so your other Tailscale devices can reach it. There is NO
# AUTH -- keep this on your tailnet. See the README security note.
HOST = os.environ.get("INTERNSTASH_HOST", "0.0.0.0")
PORT = int(os.environ.get("INTERNSTASH_PORT", "1337"))

# --- Application statuses --------------------------------------------------
# "suggested" is the triage inbox: it is what the scheduled fetcher writes, and
# also where manually-added entries start.
#
# BOARD_STATUSES get a column and are always returned by the API.
# ARCHIVED_STATUSES are excluded from every query until you ask for them -- they
# are outcomes you never revisit, and a board full of them is noise.
#
# "rejected" is deliberately NOT archived. It is the outcome of work you actually
# did, worth seeing next to the rest, so it is a board column -- just one you can
# fold away when you want the board to be only what is still live. See
# COLLAPSIBLE_STATUSES.
#
# "not_interested" matters for more than tidiness: keeping the row in the
# database preserves its external_id, which is what stops the feed re-importing
# a posting you have already passed on. Deleting the row instead would let it
# come straight back on the next fetch.
BOARD_STATUSES = (
    "suggested",
    "applied",
    "oa",
    "interviewing",
    "offer",
    "rejected",
)
ARCHIVED_STATUSES = (
    "not_interested",
)

# Board columns the UI can fold away on request. Their rows still come back from
# the API, so the toggle is pure client-side layout -- the count stays live in
# the button while the column is hidden, and toggling needs no refetch.
COLLAPSIBLE_STATUSES = (
    "rejected",
)

STATUSES = BOARD_STATUSES + ARCHIVED_STATUSES
DEFAULT_STATUS = "suggested"

# Human-facing labels; the stored values stay snake_case.
STATUS_LABELS = {
    "suggested": "suggested",
    "applied": "applied",
    "oa": "OA",
    "interviewing": "interviewing",
    "offer": "offer",
    "rejected": "rejected",
    "not_interested": "not interested",
}

# --- Season auto-triage ----------------------------------------------------
# Postings for these terms are imported straight to 'not_interested' with the
# season recorded in notes, instead of landing in your triage inbox.
#
# This applies at IMPORT time only, and the one-time backfill in migration 3
# touches nothing outside 'suggested'. Moving something back out by hand is
# permanent -- the rule will not re-archive it.
#
# Set to an empty string to disable.
AUTO_NOT_INTERESTED_SEASONS = [
    s.strip()
    for s in os.environ.get("INTERNSTASH_AUTO_NOT_INTERESTED_SEASONS", "Fall 2026").split(",")
    if s.strip()
]

# --- Tag canonicalization --------------------------------------------------
# The LLM and the feed produce near-synonyms ("swe" / "software-engineering" /
# "software-development") that would otherwise become separate filter
# checkboxes competing for the same jobs.
#
# This collapses them for DISPLAY AND FILTERING ONLY -- the original tag text
# stays untouched in the database, so editing this map re-groups old entries
# retroactively and nothing is ever lost.
#
# Edit freely: key = the tag you want to see, value = variants that fold into it.
# Variants may be written in any spelling -- both sides get normalized, so
# "Data & ML/AI" matches the stored "data-&-ml/ai".
TAG_SYNONYMS: dict[str, list[str]] = {
    "swe": [
        "software", "software-engineering", "software-development",
        "software-engineer", "sde", "programming", "coding",
    ],
    "mle": [
        # "Data & ML/AI" is the upstream feed's own category name.
        "Data & ML/AI", "machine-learning", "ml", "ai", "artificial-intelligence",
        "deep-learning", "mlops", "ml-engineering", "nlp", "computer-vision",
        "llms", "llm", "genai",
    ],
    "data": [
        "data-science", "data-analytics", "data-engineering", "analytics",
        "data-analysis", "datascience", "business-intelligence",
    ],
    "health-tech": [
        "health", "healthcare", "health-care", "clinical", "medical",
        "digital-health", "medtech", "clinicalai", "clinical-ai", "oncology",
    ],
    "bio": [
        "biotech", "bioinformatics", "biomedical", "genomics", "genomic",
        "life-sciences", "biology", "computational-biology", "neuro",
    ],
    "fintech": [
        "finance", "financial-services", "trading", "quant",
        "quantitative", "banking", "quantitative-research",
    ],
    "infra": [
        "devops", "infrastructure", "cloud", "platform", "sre",
    ],
    "hardware": [
        "embedded", "firmware", "robotics", "electrical-engineering", "asic",
        "fpga", "silicon",
    ],
}

# --- Scheduled fetch -------------------------------------------------------
# Public feeds of Summer 2027 / Fall 2026 tech internships. Each has its own
# JSON shape, so fetcher.py carries a per-source adapter keyed on "kind" --
# adding a source means one URL here plus one adapter there.
#
# zshah101: https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships
FEED_URL = os.environ.get(
    "INTERNSTASH_FEED_URL",
    "https://zshah101.github.io/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/api/jobs.json",
)

# vanshb03: https://github.com/vanshb03/Summer2027-Internships
# Raw listings feed behind the README table -- same one the repo's own
# update_readmes.py script reads to render 🔒 for closed postings.
FEED_URL_VANSHB03 = os.environ.get(
    "INTERNSTASH_FEED_URL_VANSHB03",
    "https://raw.githubusercontent.com/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
)

# One entry per source. "kind" selects the adapter in fetcher.py that maps the
# source's raw record onto our canonical job dict.
FEED_SOURCES: list[dict[str, str]] = [
    {"name": "zshah101", "kind": "zshah101", "url": FEED_URL},
    {"name": "vanshb03", "kind": "vanshb03", "url": FEED_URL_VANSHB03},
]

# How often to poll, in minutes. 0 disables the background loop entirely (you
# can still trigger a run by hand from the UI or POST /api/fetch/run).
FETCH_INTERVAL_MINUTES = int(os.environ.get("INTERNSTASH_FETCH_INTERVAL_MINUTES", "360"))

# Run one fetch shortly after startup instead of waiting a full interval.
FETCH_ON_STARTUP = os.environ.get("INTERNSTASH_FETCH_ON_STARTUP", "1") not in (
    "0",
    "false",
    "False",
    "",
)

# Case-insensitive SUBSTRING match against company + title + category + skills.
# Substring (not word-boundary) is deliberate: "bio" is meant to catch
# "biotech" / "biomedical" / "bioinformatics". The tradeoff is the occasional
# false positive, which is cheap -- suggestions land in a triage column.
FETCH_KEYWORDS = [
    kw.strip().lower()
    for kw in os.environ.get(
        "INTERNSTASH_FETCH_KEYWORDS",
        "python,machine learning,health,clinical,bio",
    ).split(",")
    if kw.strip()
]

# Network timeout for the feed poll, in seconds.
FETCH_TIMEOUT_SECONDS = int(os.environ.get("INTERNSTASH_FETCH_TIMEOUT_SECONDS", "30"))

# Case-insensitive substring match against the role title only. A match here
# means "never suggest this", overriding FETCH_KEYWORDS -- e.g. postings
# explicitly for PhD candidates, which as an intern you cannot take anyway.
# Applied at fetch time (skips insertion) AND retroactively against anything
# still sitting in 'suggested' -- moved-on rows are left alone either way.
FETCH_EXCLUDE_KEYWORDS = [
    kw.strip().lower()
    for kw in os.environ.get("INTERNSTASH_FETCH_EXCLUDE_KEYWORDS", "phd").split(",")
    if kw.strip()
]

# --- Local LLM autofill (optional) -----------------------------------------
# Paste a job description and have company/role/deadline/tags filled in by a
# local Ollama model. Entirely optional: with Ollama absent the app works
# exactly as before, minus the autofill button.
#
# Nothing is sent off the machine -- that is the point of using a local model
# rather than a hosted API for what are, after all, your job applications.
OLLAMA_ENABLED = os.environ.get("INTERNSTASH_OLLAMA_ENABLED", "1") not in (
    "0",
    "false",
    "False",
    "",
)
OLLAMA_URL = os.environ.get("INTERNSTASH_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("INTERNSTASH_OLLAMA_MODEL", "qwen2.5:7b-instruct-q4_K_M")

# First call after a cold start pays a model-load penalty (~10s); warm calls
# land around 1-2s. The generous timeout covers the cold case.
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("INTERNSTASH_OLLAMA_TIMEOUT_SECONDS", "90"))

# How long Ollama keeps the model resident after a request.
OLLAMA_KEEP_ALIVE = os.environ.get("INTERNSTASH_OLLAMA_KEEP_ALIVE", "15m")

OLLAMA_NUM_CTX = int(os.environ.get("INTERNSTASH_OLLAMA_NUM_CTX", "8192"))

# Job descriptions get truncated to this many characters before extraction.
# The identifying fields are essentially always near the top of a posting.
OLLAMA_MAX_CHARS = int(os.environ.get("INTERNSTASH_OLLAMA_MAX_CHARS", "6000"))


def ensure_data_dir() -> None:
    """Create DATA_DIR (and the DB's parent, if relocated) on demand."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
