"""Scheduled poll of the public internship feeds.

Pulls each feed in config.FEED_SOURCES, keeps postings matching
FETCH_KEYWORDS, and inserts new ones with status 'suggested' for triage. Uses
stdlib urllib -- no HTTP client dependency.

Each source has its own JSON shape; ADAPTERS maps a source's raw record onto
the canonical job dict the rest of this module (matches_keywords, _to_row)
already knows how to handle. Adding a source is one URL in config.py plus one
adapter function here.

Dedupe is handled by the UNIQUE constraint on applications.external_id, which
carries a stable id -- either the feed's own (e.g.
"greenhouse:pdtpartners:8077685") or, for sources whose ids aren't already
namespaced, one we prefix ourselves (e.g. "vanshb03:<uuid>"). Re-runs are
therefore idempotent: an already-seen posting is skipped even if you have
since moved it to another status or deleted its fields.

Two cleanup passes run after every fetch, both scoped to status='suggested'
so anything already triaged is never touched:

  - Padlock rule: vanshb03's feed marks a posting `active: false` once its
    README shows the closed-application padlock. A posting that closed after
    we suggested it gets pulled from triage; one that was already closed
    when we saw it is never suggested in the first place.
  - Cross-source dedupe: a fresh suggestion whose source_url already exists
    on a row with a different status is the same opportunity you've already
    triaged elsewhere -- the redundant suggestion is dropped.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html import escape
from typing import Any

from . import config, seasons
from . import tags as tagutil
from .db import get_conn
from .models import FetchResult

log = logging.getLogger("internstash.fetcher")

# Most recent run, surfaced at GET /api/fetch/status.
last_result: FetchResult = FetchResult(ok=True, error=None)


def _haystack(job: dict[str, Any]) -> str:
    """Fields a keyword can match against, lowercased into one blob."""
    skills = job.get("skills") or []
    parts = [
        str(job.get("company") or ""),
        str(job.get("title") or ""),
        str(job.get("category") or ""),
        " ".join(str(s) for s in skills),
    ]
    return " ".join(parts).lower()


def matches_keywords(job: dict[str, Any], keywords: list[str]) -> bool:
    """True if any keyword appears in the job's searchable fields.

    An empty keyword list means "take everything".
    """
    if not keywords:
        return True
    hay = _haystack(job)
    return any(kw in hay for kw in keywords)


def _title_excluded(title: str | None) -> bool:
    """True if the role title matches an entry in FETCH_EXCLUDE_KEYWORDS."""
    if not config.FETCH_EXCLUDE_KEYWORDS or not title:
        return False
    t = title.lower()
    return any(kw in t for kw in config.FETCH_EXCLUDE_KEYWORDS)


def _adapt_zshah101(job: dict[str, Any]) -> dict[str, Any]:
    """zshah101's feed is already in our canonical shape."""
    return job


def _adapt_vanshb03(job: dict[str, Any]) -> dict[str, Any]:
    """Map a vanshb03/Summer2027-Internships listings.json record onto the
    canonical shape.

    This feed carries no category/skills breakdown -- FETCH_KEYWORDS matching
    falls back to company + title alone for these, same as it would for any
    other source missing those fields. "Other" sponsorship means "not
    stated" here, same as zshah101's own "unknown".
    """
    raw_id = str(job.get("id") or "").strip()
    sponsorship = str(job.get("sponsorship") or "").strip()
    if sponsorship == "Other":
        sponsorship = "unknown"

    posted_iso = ""
    posted = job.get("date_posted")
    try:
        posted_iso = datetime.fromtimestamp(int(posted), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        pass

    return {
        "id": f"vanshb03:{raw_id}" if raw_id else None,
        "company": job.get("company_name"),
        "title": job.get("title"),
        "location": ", ".join(str(loc) for loc in (job.get("locations") or [])),
        "category": "",
        "skills": [],
        "season": job.get("season"),
        "url": job.get("url"),
        "salary": None,
        "sponsorship": sponsorship,
        "source": "vanshb03",
        "posted_at": posted_iso,
        "first_seen_at": posted_iso,
        # Not part of the canonical shape _to_row consumes -- carried through
        # so run_fetch can apply the padlock rule below.
        "_active": bool(job.get("active", True)),
    }


ADAPTERS = {
    "zshah101": _adapt_zshah101,
    "vanshb03": _adapt_vanshb03,
}


def _to_row(job: dict[str, Any]) -> dict[str, Any]:
    """Map a feed record onto our applications columns.

    The feed carries no JD body, so we synthesize a compact summary for both
    the searchable text and the display HTML. Every interpolated value is
    escaped -- this HTML is generated, but it contains third-party strings.
    """
    company = str(job.get("company") or "Unknown").strip()
    title = str(job.get("title") or "Unknown role").strip()
    location = str(job.get("location") or "").strip()
    category = str(job.get("category") or "").strip()
    season = str(job.get("season") or "").strip()
    url = str(job.get("url") or "").strip()
    salary = job.get("salary")
    sponsorship = str(job.get("sponsorship") or "").strip()
    skills = [str(s) for s in (job.get("skills") or [])]

    fields: list[tuple[str, str]] = []
    if location:
        fields.append(("Location", location))
    if season:
        fields.append(("Season", season))
    if category:
        fields.append(("Category", category))
    if skills:
        fields.append(("Skills", ", ".join(skills)))
    if salary:
        fields.append(("Salary", str(salary)))
    if sponsorship and sponsorship != "unknown":
        fields.append(("Sponsorship", sponsorship))
    if job.get("source"):
        fields.append(("Source", str(job["source"])))

    plain_lines = [f"{company} — {title}"] + [f"{k}: {v}" for k, v in fields]
    if url:
        plain_lines.append(f"Apply: {url}")
    jd_text_plain = "\n".join(plain_lines)

    html_parts = [f"<p><strong>{escape(company)}</strong> — {escape(title)}</p>"]
    if fields:
        html_parts.append("<ul>")
        html_parts += [
            f"<li><strong>{escape(k)}:</strong> {escape(v)}</li>" for k, v in fields
        ]
        html_parts.append("</ul>")
    if url.lower().startswith(("http://", "https://")):
        html_parts.append(f'<p><a href="{escape(url, quote=True)}">View posting</a></p>')
    html_parts.append("<p><em>Imported from the public internship feed.</em></p>")

    # Prefer the feed's own first_seen date over "today" so re-imports after a
    # gap do not all collapse onto the same date_found.
    found = job.get("first_seen_at") or job.get("posted_at") or ""
    try:
        date_found = datetime.fromisoformat(found.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, AttributeError):
        date_found = date.today().isoformat()

    tags = ", ".join(t for t in [category.lower()] + [s.lower() for s in skills] if t)
    if not tags:
        # No explicit category/skills from this source -- guess from the
        # title so its cards aren't left with no tag chips at all.
        tags = ", ".join(tagutil.infer_from_text(title))

    # The feed's own season, normalized. "Not stated" becomes NULL rather than
    # being treated as a term.
    season_norm = seasons.normalize(season) or seasons.normalize(title)

    # Terms you have told us not to bother with skip triage entirely.
    unwanted = {seasons.normalize(s) for s in config.AUTO_NOT_INTERESTED_SEASONS}
    unwanted.discard(None)
    archived = season_norm is not None and season_norm in unwanted

    return {
        "company": company,
        "role_title": title,
        "status": "not_interested" if archived else "suggested",
        "date_found": date_found,
        "jd_text_plain": jd_text_plain,
        "jd_html": "".join(html_parts),
        "source_url": url if url.lower().startswith(("http://", "https://")) else None,
        "tags": tags,
        "season": season_norm,
        # Says why it was filed away without you touching it.
        "notes": season_norm if archived else "",
        "external_id": str(job.get("id") or "").strip() or None,
    }


def fetch_feed(url: str, timeout: int) -> list[dict[str, Any]]:
    """GET the feed and return its job records."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "InternStash/1.0 (self-hosted job tracker)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    return [j for j in jobs if isinstance(j, dict)]


_INSERT_SQL = """
    INSERT OR IGNORE INTO applications
        (company, role_title, status, date_found, jd_text_plain,
         jd_html, source_url, tags, season, notes, external_id)
    VALUES
        (:company, :role_title, :status, :date_found, :jd_text_plain,
         :jd_html, :source_url, :tags, :season, :notes, :external_id)
"""


def _delete_closed_suggestions(conn, closed_external_ids: set[str]) -> int:
    """Padlock rule: drop 'suggested' rows whose posting has since closed."""
    if not closed_external_ids:
        return 0
    placeholders = ",".join("?" * len(closed_external_ids))
    cur = conn.execute(
        f"DELETE FROM applications WHERE status = 'suggested' "
        f"AND external_id IN ({placeholders})",
        tuple(closed_external_ids),
    )
    return cur.rowcount


def _delete_excluded_suggestions(conn) -> int:
    """Retroactively drop 'suggested' rows matching FETCH_EXCLUDE_KEYWORDS.

    Catches rows suggested before an exclude keyword was added (or before
    this feature existed) -- otherwise the filter would only apply going
    forward. Scoped to 'suggested' like every other cleanup pass here.
    """
    if not config.FETCH_EXCLUDE_KEYWORDS:
        return 0
    rows = conn.execute(
        "SELECT id, role_title FROM applications WHERE status = 'suggested'"
    ).fetchall()
    ids = [r["id"] for r in rows if _title_excluded(r["role_title"])]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(f"DELETE FROM applications WHERE id IN ({placeholders})", ids)
    return cur.rowcount


def _dedupe_suggestions_by_url(conn) -> int:
    """Drop 'suggested' rows whose source_url already exists under some other
    status -- the same opportunity, already triaged elsewhere. Rows without a
    status of 'suggested' are never touched.
    """
    cur = conn.execute(
        """
        DELETE FROM applications
         WHERE status = 'suggested'
           AND source_url IS NOT NULL AND source_url != ''
           AND source_url IN (
               SELECT source_url FROM applications
                WHERE status != 'suggested'
                  AND source_url IS NOT NULL AND source_url != ''
           )
        """
    )
    return cur.rowcount


def run_fetch() -> FetchResult:
    """Blocking fetch + filter + insert across every configured source.

    Safe to call repeatedly: insertion is idempotent (external_id UNIQUE), and
    the cleanup passes only ever touch rows still in 'suggested'.
    """
    global last_result
    ran_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    fetched_total = 0
    matched_total = 0
    inserted_total = 0
    auto_archived_total = 0
    closed_external_ids: set[str] = set()
    errors: list[str] = []

    with get_conn() as conn:
        for src in config.FEED_SOURCES:
            try:
                raw_jobs = fetch_feed(src["url"], config.FETCH_TIMEOUT_SECONDS)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                log.warning("feed fetch failed for %s: %s", src["name"], exc)
                errors.append(f"{src['name']}: {type(exc).__name__}: {exc}")
                continue

            adapt = ADAPTERS[src["kind"]]
            jobs = [adapt(j) for j in raw_jobs]
            fetched_total += len(jobs)

            for job in jobs:
                if job.get("id") and job.get("_active") is False:
                    closed_external_ids.add(job["id"])

            matched = [
                j for j in jobs
                if j.get("_active", True)
                and matches_keywords(j, config.FETCH_KEYWORDS)
                and not _title_excluded(j.get("title"))
            ]
            matched_total += len(matched)

            for job in matched:
                row = _to_row(job)
                if not row["external_id"]:
                    continue  # no stable id -> cannot dedupe, so skip rather than duplicate
                cur = conn.execute(_INSERT_SQL, row)
                inserted_total += cur.rowcount
                if cur.rowcount and row["status"] == "not_interested":
                    auto_archived_total += 1

        closed_dropped = _delete_closed_suggestions(conn, closed_external_ids)
        excluded_dropped = _delete_excluded_suggestions(conn)
        url_deduped = _dedupe_suggestions_by_url(conn)

    # A source erroring out doesn't fail the whole run as long as at least one
    # other source came through.
    ok = len(errors) < len(config.FEED_SOURCES)
    result = FetchResult(
        ok=ok,
        fetched=fetched_total,
        matched=matched_total,
        inserted=inserted_total,
        auto_archived=auto_archived_total,
        skipped_existing=matched_total - inserted_total,
        error="; ".join(errors) or None,
        ran_at=ran_at,
    )
    log.info(
        "fetch complete: %d fetched, %d matched, %d new (%d auto-archived by season, "
        "%d closed suggestions dropped, %d excluded-keyword suggestions dropped, "
        "%d duplicate suggestions deduped)%s",
        result.fetched, result.matched, result.inserted, result.auto_archived,
        closed_dropped, excluded_dropped, url_deduped,
        f" -- errors: {result.error}" if errors else "",
    )
    last_result = result
    return result


async def run_fetch_async() -> FetchResult:
    """Run the blocking fetch off the event loop."""
    return await asyncio.to_thread(run_fetch)


async def fetch_loop() -> None:
    """Background poller. Cancelled on app shutdown."""
    if config.FETCH_ON_STARTUP:
        # Small delay so startup isn't blocked on network I/O.
        await asyncio.sleep(3)
        try:
            await run_fetch_async()
        except Exception:
            log.exception("startup fetch failed")

    interval = config.FETCH_INTERVAL_MINUTES * 60
    while True:
        await asyncio.sleep(interval)
        try:
            await run_fetch_async()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let one bad run kill the loop.
            log.exception("scheduled fetch failed")
