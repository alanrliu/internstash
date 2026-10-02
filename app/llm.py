"""Optional local-LLM field extraction via Ollama.

Paste a job description, get company / role / deadline / tags / source URL
filled in. Runs entirely against a local Ollama instance -- nothing leaves the
machine, which is the whole point of self-hosting this.

This module is STRICTLY OPTIONAL. If Ollama is not installed, not running, or
the model is missing, extraction degrades to "unavailable" and the rest of the
app is unaffected. Anyone cloning this repo without Ollama gets a working
tracker with manual entry.

Uses stdlib urllib -- no new Python dependency.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Optional

from . import config
from . import seasons as _seasons

log = logging.getLogger("internstash.llm")

# Ollama structured-output schema. Passing this as `format` constrains decoding
# to valid JSON of this shape, so we do not have to parse prose.
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "role_title": {"type": "string"},
        "deadline": {"type": ["string", "null"]},
        "source_url": {"type": ["string", "null"]},
        "season": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["company", "role_title", "deadline", "source_url", "season", "tags"],
}

PROMPT_TEMPLATE = """Extract structured fields from this job posting.

Rules:
- company: the hiring organization's name only. Strip legal suffixes like "Inc.", "LLC", "Ltd".
- role_title: the job title, copied as closely as possible from the posting. Do not
  include the company or location.
- deadline: the application deadline as YYYY-MM-DD. null if the posting does not state one.
- source_url: the application URL if one literally appears in the text. null otherwise.
  Never guess or construct a URL.
- season: the internship term, exactly as "Summer 2027" / "Fall 2026" / "Spring 2027".
  null if the posting does not state a term. Do not guess from the deadline.
- tags: 2 to 5 short lowercase topic tags describing the domain and discipline,
  e.g. "health-tech", "mle", "swe", "bio", "genomics", "fintech".
  Do NOT include generic words like "internship", "intern", "tech", "job", or "hiring".
- If a field is not present in the posting, use null. Never invent values.

JOB POSTING:
{jd}"""

# Ollama returns dates in assorted shapes; we only accept a clean ISO date.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_GENERIC_TAGS = {
    "internship", "intern", "tech", "job", "hiring", "career", "careers",
    "full-time", "part-time", "student", "summer", "fall",
}


def is_enabled() -> bool:
    return config.OLLAMA_ENABLED


def health() -> dict[str, Any]:
    """Report whether extraction is usable, and why not if it isn't.

    Surfaced at /api/meta so the frontend can hide the autofill affordance
    instead of offering a button that will fail.
    """
    if not config.OLLAMA_ENABLED:
        return {"available": False, "reason": "disabled by config"}
    try:
        req = urllib.request.Request(f"{config.OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"Ollama unreachable at {config.OLLAMA_URL} ({type(exc).__name__})"}

    names = [m.get("name", "") for m in data.get("models", [])]
    if not names:
        return {"available": False, "reason": "Ollama is running but has no models pulled"}

    # Accept an exact match or a bare name that matches before the tag, so
    # "qwen2.5:7b-instruct-q4_K_M" satisfies a configured "qwen2.5".
    want = config.OLLAMA_MODEL
    if want in names or any(n.split(":")[0] == want.split(":")[0] for n in names):
        return {"available": True, "model": want, "url": config.OLLAMA_URL}
    return {
        "available": False,
        "reason": f"model '{want}' not found. Pulled: {', '.join(names)}",
        "models": names,
    }


def _clean_tags(raw: Any) -> str:
    """Normalize the model's tag list into our comma-separated string."""
    if not isinstance(raw, list):
        return ""
    out: list[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        tag = t.strip().lower().replace(" ", "-")
        if not tag or tag in _GENERIC_TAGS or tag in out:
            continue
        out.append(tag)
    return ", ".join(out[:5])


def _clean_url(raw: Any) -> Optional[str]:
    """Only accept a real http(s) URL; drop anything else the model invented."""
    if not isinstance(raw, str):
        return None
    url = raw.strip()
    return url if url.lower().startswith(("http://", "https://")) else None


def _clean_date(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    d = raw.strip()
    return d if _ISO_DATE.match(d) else None


def extract_fields(jd_text: str) -> dict[str, Any]:
    """Ask the local model to pull fields out of a pasted job description.

    Returns {"ok": bool, ...fields} -- never raises for the caller's benefit,
    since a failed autofill should degrade to manual entry, not a 500.
    """
    text = (jd_text or "").strip()
    if not text:
        return {"ok": False, "error": "empty job description"}

    if not config.OLLAMA_ENABLED:
        return {"ok": False, "error": "LLM autofill is disabled"}

    # Long postings blow past the context window and slow things down; the
    # identifying fields are essentially always near the top.
    truncated = text[: config.OLLAMA_MAX_CHARS]

    payload = json.dumps({
        "model": config.OLLAMA_MODEL,
        "prompt": PROMPT_TEMPLATE.format(jd=truncated),
        "format": EXTRACT_SCHEMA,
        "stream": False,
        # Deterministic: same paste should give the same fields.
        "options": {"temperature": 0, "num_ctx": config.OLLAMA_NUM_CTX},
        # Keep the model resident so later pastes skip the cold-load penalty.
        "keep_alive": config.OLLAMA_KEEP_ALIVE,
    }).encode()

    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=config.OLLAMA_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read())
        parsed = json.loads(body["response"])
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("ollama request failed: %s", exc)
        return {"ok": False, "error": f"Ollama unreachable ({type(exc).__name__})"}
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("ollama returned unusable output: %s", exc)
        return {"ok": False, "error": "model returned unparseable output"}

    company = (parsed.get("company") or "").strip()
    role = (parsed.get("role_title") or "").strip()

    return {
        "ok": True,
        "company": company,
        "role_title": role,
        "deadline": _clean_date(parsed.get("deadline")),
        "season": _seasons.normalize(parsed.get("season")),
        "source_url": _clean_url(parsed.get("source_url")),
        "tags": _clean_tags(parsed.get("tags")),
        "model": config.OLLAMA_MODEL,
    }
