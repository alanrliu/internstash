"""FastAPI application: routes for the tracker plus the static frontend.

There is NO AUTHENTICATION here by design -- this is meant to run on a private
Tailscale network. Do not expose it to the public internet without putting
real auth in front of it. See README.
"""

import hashlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, fetcher, llm
# Aliased: the `tags` query parameter below would otherwise shadow the module.
from . import tags as tagutil
from . import seasons
from .models import (
    Application,
    ApplicationCreate,
    ApplicationUpdate,
    FetchResult,
)
from .sanitize import html_to_text, sanitize_html

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("internstash")

COLUMNS = (
    "id, company, role_title, status, date_found, date_applied, deadline, "
    "jd_text_plain, jd_html, source_url, notes, tags, season, external_id, "
    "idempotency_key, created_at, updated_at"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_data_dir()
    version = db.migrate()
    log.info("database ready at %s (schema v%d)", config.DB_PATH, version)

    task = None
    if config.FETCH_INTERVAL_MINUTES > 0:
        import asyncio

        task = asyncio.create_task(fetcher.fetch_loop())
        log.info(
            "fetch loop every %d min, keywords=%s",
            config.FETCH_INTERVAL_MINUTES,
            config.FETCH_KEYWORDS,
        )
    else:
        log.info("scheduled fetch disabled (interval=0); trigger manually if needed")

    yield

    if task:
        task.cancel()


app = FastAPI(title="InternStash", version="1.0.0", lifespan=lifespan)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------


@app.get("/api/applications", response_model=list[Application])
def list_applications(
    q: Optional[str] = Query(default=None, description="Full-text search"),
    status: Optional[str] = Query(default=None),
    tags: Optional[str] = Query(
        default=None, description="Comma-separated canonical tags; matches ANY (OR)"
    ),
    season: Optional[str] = Query(
        default=None,
        description="Comma-separated terms, e.g. 'Summer 2027'. Use 'none' for unstated.",
    ),
    include_archived: bool = Query(
        default=False, description="Include archived statuses (not_interested)"
    ),
) -> list[dict[str, Any]]:
    """List applications, optionally full-text and tag filtered.

    Archived statuses (not_interested) are excluded by default so the board
    stays to the columns you act on. Asking for one explicitly by name still
    returns it. "rejected" is a board status and always comes back -- hiding
    that column is the frontend's business, not a query.

    Sorted so soonest deadline floats to the top of each column, with
    deadline-less entries after, most recently touched first.
    """
    where: list[str] = []
    params: list[Any] = []

    if q and q.strip():
        match = db.fts_query(q)
        if match:
            where.append(
                "a.id IN (SELECT rowid FROM applications_fts "
                "WHERE applications_fts MATCH ?)"
            )
            params.append(match)

    if status:
        if status not in config.STATUSES:
            raise HTTPException(400, f"unknown status: {status}")
        where.append("a.status = ?")
        params.append(status)
    elif not include_archived:
        placeholders = ",".join("?" * len(config.ARCHIVED_STATUSES))
        where.append(f"a.status NOT IN ({placeholders})")
        params.extend(config.ARCHIVED_STATUSES)

    sql = f"SELECT {COLUMNS} FROM applications a"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += """
        ORDER BY
            CASE WHEN a.deadline IS NULL OR a.deadline = '' THEN 1 ELSE 0 END,
            a.deadline ASC,
            a.updated_at DESC
    """

    with db.get_conn() as conn:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # Malformed FTS expression -- surface as a 400, not a 500.
            raise HTTPException(400, f"bad search query: {exc}") from exc

    result = [_row_to_dict(r) for r in rows]

    # Tag filtering happens in Python because it compares CANONICAL tags, which
    # SQL knows nothing about. The dataset is small enough that this is cheap.
    if tags and tags.strip():
        wanted = [t.strip() for t in tags.split(",") if t.strip()]
        if wanted:
            result = [r for r in result if tagutil.matches_any(r["tags"], wanted)]

    # Season is a separate axis from tags: a term is a scale, not a topic. The
    # literal "none" selects postings that never stated one.
    if season and season.strip():
        wanted_seasons: set[Optional[str]] = set()
        for part in season.split(","):
            part = part.strip()
            if not part:
                continue
            if part.lower() == "none":
                wanted_seasons.add(None)
            else:
                wanted_seasons.add(seasons.normalize(part))
        if wanted_seasons:
            result = [
                r for r in result if seasons.normalize(r["season"]) in wanted_seasons
            ]

    return result


@app.get("/api/applications/{app_id}", response_model=Application)
def get_application(app_id: int) -> dict[str, Any]:
    with db.get_conn() as conn:
        row = conn.execute(
            f"SELECT {COLUMNS} FROM applications a WHERE a.id = ?", (app_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "application not found")
    return _row_to_dict(row)


@app.post("/api/applications", response_model=Application, status_code=201)
def create_application(payload: ApplicationCreate, response: Response) -> dict[str, Any]:
    """Create an application, at most once per idempotency key.

    Spam-clicking Save must not create six rows. The frontend disables the
    button while a save is in flight, but that alone is not a guarantee -- a JS
    error, a second tab, or a browser retry can all still fire a duplicate
    request. The key is the actual contract: re-POSTing one returns the row
    created the first time, with 200 instead of 201.
    """
    clean_html = sanitize_html(payload.jd_html)
    plain = payload.jd_text_plain.strip() or html_to_text(clean_html)
    key = (payload.idempotency_key or "").strip() or None

    with db.get_conn() as conn:
        if key:
            existing = conn.execute(
                f"SELECT {COLUMNS} FROM applications a WHERE a.idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                response.status_code = 200  # nothing new was created
                return _row_to_dict(existing)

        try:
            cur = conn.execute(
                """
                INSERT INTO applications
                    (company, role_title, status, date_found, date_applied, deadline,
                     jd_text_plain, jd_html, source_url, notes, tags, season,
                     idempotency_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    payload.company.strip(),
                    payload.role_title.strip(),
                    payload.status,
                    payload.date_found,
                    payload.date_applied,
                    payload.deadline,
                    plain,
                    clean_html,
                    payload.source_url,
                    payload.notes,
                    payload.tags.strip(),
                    payload.season,
                    key,
                ),
            )
            new_id = cur.lastrowid
        except sqlite3.IntegrityError:
            # Two requests with the same key raced past the SELECT above; the
            # unique index let exactly one win. Return the winner's row.
            if not key:
                raise
            row = conn.execute(
                f"SELECT {COLUMNS} FROM applications a WHERE a.idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise
            response.status_code = 200
            return _row_to_dict(row)

        row = conn.execute(
            f"SELECT {COLUMNS} FROM applications a WHERE a.id = ?", (new_id,)
        ).fetchone()
    return _row_to_dict(row)


@app.patch("/api/applications/{app_id}", response_model=Application)
def update_application(app_id: int, payload: ApplicationUpdate) -> dict[str, Any]:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(400, "no fields to update")

    # Sanitize before storage; re-derive plain text if the caller replaced the
    # HTML without supplying matching plain text.
    if "jd_html" in updates:
        updates["jd_html"] = sanitize_html(updates["jd_html"])
        if not (updates.get("jd_text_plain") or "").strip():
            updates["jd_text_plain"] = html_to_text(updates["jd_html"])

    for key in ("company", "role_title", "tags"):
        if key in updates and isinstance(updates[key], str):
            updates[key] = updates[key].strip()

    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = app_id

    with db.get_conn() as conn:
        cur = conn.execute(
            f"UPDATE applications SET {assignments}, updated_at = datetime('now') "
            "WHERE id = :id",
            updates,
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "application not found")
        row = conn.execute(
            f"SELECT {COLUMNS} FROM applications a WHERE a.id = ?", (app_id,)
        ).fetchone()
    return _row_to_dict(row)


@app.delete("/api/applications/{app_id}", status_code=204)
def delete_application(app_id: int) -> None:
    with db.get_conn() as conn:
        cur = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "application not found")


# --------------------------------------------------------------------------
# Fetch job
# --------------------------------------------------------------------------


@app.post("/api/fetch/run", response_model=FetchResult)
async def trigger_fetch() -> FetchResult:
    """Run the feed import now, without waiting for the schedule."""
    return await fetcher.run_fetch_async()


@app.get("/api/fetch/status", response_model=FetchResult)
def fetch_status() -> FetchResult:
    return fetcher.last_result


# --------------------------------------------------------------------------
# Meta + frontend
# --------------------------------------------------------------------------


@app.post("/api/sanitize")
def sanitize_endpoint(payload: dict[str, str]) -> dict[str, str]:
    """Clean a chunk of HTML and hand it back.

    The paste box calls this before putting clipboard HTML into the live DOM.
    Inserting raw clipboard HTML would be an XSS sink in its own right (script
    tags stay inert via innerHTML, but inline handlers like onerror still
    fire), and this keeps nh3 as the single sanitizer rather than reimplementing
    an allowlist in JavaScript.
    """
    raw = payload.get("html", "")
    clean = sanitize_html(raw)
    return {"html": clean, "text": html_to_text(clean)}


@app.post("/api/extract")
async def extract(payload: dict[str, str]) -> dict[str, Any]:
    """Pull structured fields out of a pasted job description, via local Ollama.

    Best-effort by design: a failure here returns ok=false rather than an HTTP
    error, so the form falls back to manual entry instead of blocking the user.
    """
    import asyncio

    text = payload.get("text", "")
    # Blocking urllib call; keep it off the event loop.
    return await asyncio.to_thread(llm.extract_fields, text)


@app.get("/api/llm/health")
def llm_health() -> dict[str, Any]:
    return llm.health()


@app.get("/api/tags")
def list_tags(include_archived: bool = Query(default=False)) -> list[dict[str, Any]]:
    """Canonical tags with counts, for the filter checkboxes.

    Variants that folded into each canonical tag are returned too, so the UI can
    explain why "software-engineering" is filed under "swe".
    """
    sql = "SELECT tags, status FROM applications"
    params: list[Any] = []
    if not include_archived:
        placeholders = ",".join("?" * len(config.ARCHIVED_STATUSES))
        sql += f" WHERE status NOT IN ({placeholders})"
        params.extend(config.ARCHIVED_STATUSES)

    with db.get_conn() as conn:
        rows = [{"tags": r["tags"]} for r in conn.execute(sql, params)]
    return tagutil.summarize(rows)


@app.get("/api/seasons")
def list_seasons(include_archived: bool = Query(default=False)) -> list[dict[str, Any]]:
    """Season facets with counts, in chronological order."""
    sql = "SELECT season FROM applications"
    params: list[Any] = []
    if not include_archived:
        placeholders = ",".join("?" * len(config.ARCHIVED_STATUSES))
        sql += f" WHERE status NOT IN ({placeholders})"
        params.extend(config.ARCHIVED_STATUSES)

    with db.get_conn() as conn:
        rows = [{"season": r["season"]} for r in conn.execute(sql, params)]
    return seasons.summarize(rows)


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    with db.get_conn() as conn:
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
            )
        }
    return {
        "statuses": list(config.STATUSES),
        "board_statuses": list(config.BOARD_STATUSES),
        "archived_statuses": list(config.ARCHIVED_STATUSES),
        "collapsible_statuses": list(config.COLLAPSIBLE_STATUSES),
        "status_labels": config.STATUS_LABELS,
        "counts": {s: counts.get(s, 0) for s in config.STATUSES},
        "fetch": {
            "feed_sources": [
                {"name": s["name"], "url": s["url"]} for s in config.FEED_SOURCES
            ],
            "keywords": config.FETCH_KEYWORDS,
            "interval_minutes": config.FETCH_INTERVAL_MINUTES,
        },
        # The frontend hides the autofill affordance entirely when this is
        # unavailable, rather than offering a button that cannot work.
        "llm": llm.health(),
        "today": date.today().isoformat(),
        "build": build_stamp(),
    }


@app.get("/api/healthz")
def healthz() -> JSONResponse:
    return JSONResponse({"ok": True, "db": str(config.DB_PATH)})


# Revalidate-always cache policy for everything the browser loads.
#
# With no Cache-Control header, browsers fall back to HEURISTIC caching: a soft
# reload (F5) may serve app.js from memory cache without ever contacting the
# server, so a shipped fix silently never reaches the page and the user is told
# to "hard-refresh" forever. "no-cache" does not mean "do not store" -- it means
# "always revalidate first", so the ETag above still makes the common case a
# cheap 304 with no body.
NO_CACHE = "no-cache, must-revalidate"


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that forces revalidation instead of heuristic caching."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = NO_CACHE
        return response


def build_stamp() -> str:
    """Short fingerprint of the frontend files, so which build is live is checkable."""
    digest = hashlib.sha256()
    for f in sorted(config.STATIC_DIR.glob("*")):
        digest.update(f.name.encode())
        digest.update(str(f.stat().st_mtime_ns).encode())
    return digest.hexdigest()[:8]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(
        config.STATIC_DIR / "index.html",
        headers={"Cache-Control": NO_CACHE},
    )


app.mount("/static", RevalidatingStaticFiles(directory=config.STATIC_DIR), name="static")
