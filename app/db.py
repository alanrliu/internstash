"""SQLite connection handling and schema migrations.

Migrations are plain SQL keyed off SQLite's built-in `user_version` pragma.
To add one: append to MIGRATIONS and never edit an entry that has shipped.
"""

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

# Each entry is one migration, applied in order. Index + 1 == user_version.
MIGRATIONS: list[str] = [
    # --- 1: initial schema -------------------------------------------------
    """
    CREATE TABLE applications (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        company       TEXT NOT NULL,
        role_title    TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'saved',

        date_found    TEXT,   -- ISO-8601 date (YYYY-MM-DD)
        date_applied  TEXT,
        deadline      TEXT,

        jd_text_plain TEXT NOT NULL DEFAULT '',  -- searchable body
        jd_html       TEXT NOT NULL DEFAULT '',  -- sanitized, for display

        source_url    TEXT,
        notes         TEXT NOT NULL DEFAULT '',
        tags          TEXT NOT NULL DEFAULT '',  -- comma-separated

        -- Stable id from the upstream feed (e.g. "greenhouse:acme:123").
        -- NULL for hand-entered rows. UNIQUE gives us idempotent fetches.
        external_id   TEXT UNIQUE,

        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now')),

        CHECK (status IN (
            'suggested','saved','applied','interviewing','offer','rejected'
        ))
    );

    CREATE INDEX idx_applications_status   ON applications(status);
    CREATE INDEX idx_applications_deadline ON applications(deadline);
    CREATE INDEX idx_applications_company  ON applications(company);

    -- Full-text index. `content=` makes this an external-content table: FTS5
    -- stores only the index, not a second copy of the text. The triggers below
    -- keep it in sync.
    CREATE VIRTUAL TABLE applications_fts USING fts5(
        company,
        role_title,
        jd_text_plain,
        tags,
        content='applications',
        content_rowid='id',
        tokenize='unicode61'
    );

    CREATE TRIGGER applications_ai AFTER INSERT ON applications BEGIN
        INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        VALUES (new.id, new.company, new.role_title, new.jd_text_plain, new.tags);
    END;

    CREATE TRIGGER applications_ad AFTER DELETE ON applications BEGIN
        INSERT INTO applications_fts(applications_fts, rowid, company, role_title, jd_text_plain, tags)
        VALUES ('delete', old.id, old.company, old.role_title, old.jd_text_plain, old.tags);
    END;

    CREATE TRIGGER applications_au AFTER UPDATE ON applications BEGIN
        INSERT INTO applications_fts(applications_fts, rowid, company, role_title, jd_text_plain, tags)
        VALUES ('delete', old.id, old.company, old.role_title, old.jd_text_plain, old.tags);
        INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        VALUES (new.id, new.company, new.role_title, new.jd_text_plain, new.tags);
    END;
    """,
    # --- 2: drop 'saved', add hidden 'not_interested' ----------------------
    # SQLite cannot ALTER a CHECK constraint, so the table is rebuilt. The FTS
    # index and its triggers reference this table, so they come down first and
    # are rebuilt from scratch afterwards.
    """
    DROP TRIGGER IF EXISTS applications_ai;
    DROP TRIGGER IF EXISTS applications_ad;
    DROP TRIGGER IF EXISTS applications_au;
    DROP TABLE IF EXISTS applications_fts;

    CREATE TABLE applications_new (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        company       TEXT NOT NULL,
        role_title    TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'suggested',

        date_found    TEXT,
        date_applied  TEXT,
        deadline      TEXT,

        jd_text_plain TEXT NOT NULL DEFAULT '',
        jd_html       TEXT NOT NULL DEFAULT '',

        source_url    TEXT,
        notes         TEXT NOT NULL DEFAULT '',
        tags          TEXT NOT NULL DEFAULT '',

        external_id   TEXT UNIQUE,

        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now')),

        CHECK (status IN (
            'suggested','applied','interviewing','offer','rejected','not_interested'
        ))
    );

    INSERT INTO applications_new
        (id, company, role_title, status, date_found, date_applied, deadline,
         jd_text_plain, jd_html, source_url, notes, tags, external_id,
         created_at, updated_at)
    SELECT
        id, company, role_title,
        -- 'saved' meant "found it, haven't applied yet"; the triage inbox is
        -- its closest equivalent in the new model.
        CASE status WHEN 'saved' THEN 'suggested' ELSE status END,
        date_found, date_applied, deadline,
        jd_text_plain, jd_html, source_url, notes, tags, external_id,
        created_at, updated_at
    FROM applications;

    DROP TABLE applications;
    ALTER TABLE applications_new RENAME TO applications;

    CREATE INDEX idx_applications_status   ON applications(status);
    CREATE INDEX idx_applications_deadline ON applications(deadline);
    CREATE INDEX idx_applications_company  ON applications(company);

    CREATE VIRTUAL TABLE applications_fts USING fts5(
        company,
        role_title,
        jd_text_plain,
        tags,
        content='applications',
        content_rowid='id',
        tokenize='unicode61'
    );

    INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        SELECT id, company, role_title, jd_text_plain, tags FROM applications;

    CREATE TRIGGER applications_ai AFTER INSERT ON applications BEGIN
        INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        VALUES (new.id, new.company, new.role_title, new.jd_text_plain, new.tags);
    END;

    CREATE TRIGGER applications_ad AFTER DELETE ON applications BEGIN
        INSERT INTO applications_fts(applications_fts, rowid, company, role_title, jd_text_plain, tags)
        VALUES ('delete', old.id, old.company, old.role_title, old.jd_text_plain, old.tags);
    END;

    CREATE TRIGGER applications_au AFTER UPDATE ON applications BEGIN
        INSERT INTO applications_fts(applications_fts, rowid, company, role_title, jd_text_plain, tags)
        VALUES ('delete', old.id, old.company, old.role_title, old.jd_text_plain, old.tags);
        INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        VALUES (new.id, new.company, new.role_title, new.jd_text_plain, new.tags);
    END;
    """,
    # --- 3: season column, backfill, and one-time Fall-2026 triage ---------
    lambda conn: _migrate_seasons(conn),
    # --- 4: idempotency key, so a double-submit cannot double-insert -------
    # SQLite forbids UNIQUE in ALTER TABLE ADD COLUMN, so the constraint is
    # added as a separate unique index. NULLs are exempt from uniqueness in
    # SQLite, so every pre-existing row (and any client that omits a key)
    # remains valid.
    """
    ALTER TABLE applications ADD COLUMN idempotency_key TEXT;
    CREATE UNIQUE INDEX idx_applications_idem
        ON applications(idempotency_key);
    """,
    # --- 5: old 'interviewing' becomes 'oa'; new 'interviewing' after it --
    # Same CHECK-constraint rebuild as migration 2. Every existing
    # 'interviewing' row was really an online assessment, so it moves to 'oa';
    # the new 'interviewing' column starts empty.
    """
    DROP TRIGGER IF EXISTS applications_ai;
    DROP TRIGGER IF EXISTS applications_ad;
    DROP TRIGGER IF EXISTS applications_au;
    DROP TABLE IF EXISTS applications_fts;

    CREATE TABLE applications_new (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        company         TEXT NOT NULL,
        role_title      TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'suggested',

        date_found      TEXT,
        date_applied    TEXT,
        deadline        TEXT,

        jd_text_plain   TEXT NOT NULL DEFAULT '',
        jd_html         TEXT NOT NULL DEFAULT '',

        source_url      TEXT,
        notes           TEXT NOT NULL DEFAULT '',
        tags            TEXT NOT NULL DEFAULT '',

        external_id     TEXT UNIQUE,

        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),

        season          TEXT,
        idempotency_key TEXT,

        CHECK (status IN (
            'suggested','applied','oa','interviewing','offer','rejected','not_interested'
        ))
    );

    INSERT INTO applications_new
        (id, company, role_title, status, date_found, date_applied, deadline,
         jd_text_plain, jd_html, source_url, notes, tags, external_id,
         created_at, updated_at, season, idempotency_key)
    SELECT
        id, company, role_title,
        CASE status WHEN 'interviewing' THEN 'oa' ELSE status END,
        date_found, date_applied, deadline,
        jd_text_plain, jd_html, source_url, notes, tags, external_id,
        created_at, updated_at, season, idempotency_key
    FROM applications;

    DROP TABLE applications;
    ALTER TABLE applications_new RENAME TO applications;

    CREATE INDEX idx_applications_status   ON applications(status);
    CREATE INDEX idx_applications_deadline ON applications(deadline);
    CREATE INDEX idx_applications_company  ON applications(company);
    CREATE INDEX idx_applications_season   ON applications(season);
    CREATE UNIQUE INDEX idx_applications_idem
        ON applications(idempotency_key);

    CREATE VIRTUAL TABLE applications_fts USING fts5(
        company,
        role_title,
        jd_text_plain,
        tags,
        content='applications',
        content_rowid='id',
        tokenize='unicode61'
    );

    INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        SELECT id, company, role_title, jd_text_plain, tags FROM applications;

    CREATE TRIGGER applications_ai AFTER INSERT ON applications BEGIN
        INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        VALUES (new.id, new.company, new.role_title, new.jd_text_plain, new.tags);
    END;

    CREATE TRIGGER applications_ad AFTER DELETE ON applications BEGIN
        INSERT INTO applications_fts(applications_fts, rowid, company, role_title, jd_text_plain, tags)
        VALUES ('delete', old.id, old.company, old.role_title, old.jd_text_plain, old.tags);
    END;

    CREATE TRIGGER applications_au AFTER UPDATE ON applications BEGIN
        INSERT INTO applications_fts(applications_fts, rowid, company, role_title, jd_text_plain, tags)
        VALUES ('delete', old.id, old.company, old.role_title, old.jd_text_plain, old.tags);
        INSERT INTO applications_fts(rowid, company, role_title, jd_text_plain, tags)
        VALUES (new.id, new.company, new.role_title, new.jd_text_plain, new.tags);
    END;
    """,
]


def _migrate_seasons(conn: sqlite3.Connection) -> None:
    """Add `season`, backfill it, and archive unwanted terms -- once.

    Backfill reads the stored job-description text rather than re-fetching the
    feed: migrations should not depend on the network, and rows imported from an
    older feed snapshot may no longer be upstream at all.

    The auto-archive step deliberately touches ONLY rows still in 'suggested'.
    Anything you have already triaged -- applied, interviewing, or manually
    filed -- is left exactly as it is.
    """
    from . import config, seasons

    conn.execute("ALTER TABLE applications ADD COLUMN season TEXT")
    conn.execute("CREATE INDEX idx_applications_season ON applications(season)")

    rows = conn.execute(
        "SELECT id, season, role_title, jd_text_plain, notes, status FROM applications"
    ).fetchall()

    filled = 0
    for row in rows:
        if row["season"]:
            continue
        # The feed importer writes a "Season: ..." line; fall back to the title.
        found = seasons.normalize(row["jd_text_plain"]) or seasons.normalize(row["role_title"])
        if found:
            conn.execute("UPDATE applications SET season = ? WHERE id = ?", (found, row["id"]))
            filled += 1

    wanted_out = {seasons.normalize(s) for s in config.AUTO_NOT_INTERESTED_SEASONS}
    wanted_out.discard(None)
    if not wanted_out:
        return

    placeholders = ",".join("?" * len(wanted_out))
    conn.execute(
        f"""
        UPDATE applications
           SET status = 'not_interested',
               -- Record why, without discarding a note you already wrote.
               notes = CASE
                   WHEN notes IS NULL OR notes = '' THEN season
                   ELSE notes || ' | ' || season
               END,
               updated_at = datetime('now')
         WHERE status = 'suggested'
           AND season IN ({placeholders})
        """,
        tuple(wanted_out),
    )


def connect() -> sqlite3.Connection:
    """Open a connection to the configured DB with sane defaults."""
    config.ensure_data_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the background fetcher write while you browse.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Connection context manager that commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate() -> int:
    """Apply any pending migrations. Returns the resulting schema version.

    An entry may be a SQL script or a callable taking the connection, for the
    occasional migration that needs real logic (see migration 3).
    """
    with get_conn() as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current, len(MIGRATIONS)):
            step = MIGRATIONS[version]
            if callable(step):
                step(conn)
            else:
                conn.executescript(step)
            # PRAGMA does not accept bound parameters; version is a loop int.
            conn.execute(f"PRAGMA user_version = {version + 1}")
        return len(MIGRATIONS)


def fts_query(raw: str) -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    FTS5 has its own query syntax, so bare user input can be a syntax error or
    an unintended operator. We quote every token (neutralizing operators) and
    append * for prefix matching, then AND them together.
    """
    # Drop tokens with no alphanumeric content. The unicode61 tokenizer strips
    # punctuation when indexing, so a token like "&" indexes to nothing and can
    # never match -- and because tokens are ANDed, one such token would zero out
    # the whole query. Searching "Core & Main" must still find "Core & Main".
    tokens = [
        t for t in raw.replace('"', " ").split()
        if t.strip() and any(ch.isalnum() for ch in t)
    ]
    if not tokens:
        return ""
    return " AND ".join(f'"{token}"*' for token in tokens)
