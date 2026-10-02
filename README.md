# 📥 InternStash

A small, self-hosted internship & job application tracker. Kanban board, full-text
search, rich-paste job descriptions, and a scheduled importer that pulls matching
internship postings straight into a triage column.

Built for a home lab / Tailscale network. No accounts, no cloud, no telemetry —
one SQLite file you own.

```
FastAPI  ·  SQLite (FTS5)  ·  vanilla HTML/CSS/JS  ·  3 dependencies
```

---

## ⚠️ Security: there is no authentication

**This app has no login, no sessions, and no access control. Anyone who can reach
the port has full read/write access to your data.**

That is a deliberate design choice: it's meant to run on a private
[Tailscale](https://tailscale.com) network (or equivalent VPN / LAN), where the
network *is* the access control. It keeps the app simple and dependency-light.

**Do not port-forward this, put it on a public IP, or expose it through a reverse
proxy on the open internet without adding real authentication first.**

If you do need to expose it, put an authenticating proxy in front — Tailscale
Serve, Cloudflare Access, `oauth2-proxy`, or basic auth at the nginx/Caddy layer.
Adding auth is out of scope for this project.

By default the server binds `0.0.0.0` so other devices on your tailnet can reach
it. To restrict it to the local machine only, set `INTERNSTASH_HOST=127.0.0.1`.

---

## Quick start

```bash
git clone <your-fork-url> internstash
cd internstash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

Open <http://localhost:1337> — or `http://<your-tailscale-name>:1337` from any
other device on your tailnet.

The database is created automatically on first run. There is no separate
migration step.

Requires Python 3.10+.

---

## Your data lives in `data/` — and stays there

Everything the app writes at runtime goes in one gitignored folder:

```
data/
├── .gitkeep          # tracked, keeps the folder in a fresh clone
├── README.md         # tracked
└── internstash.db    # IGNORED — your applications, notes, job descriptions
```

Your application history is personal — companies, interview notes, salary
figures, referral contacts. None of it belongs in a public repo, so `.gitignore`
excludes the folder's contents from the very first commit:

```gitignore
data/*
!data/.gitkeep
!data/README.md
```

> The pattern is `data/*`, **not** a bare `data/`. Git doesn't descend into an
> excluded directory, so `data/` would make those `!` re-includes unreachable and
> the folder would vanish for anyone cloning the repo.

Verify it yourself at any time:

```bash
git check-ignore -v data/internstash.db
```

**No user-specific path is hardcoded anywhere outside [`app/config.py`](app/config.py).**
That file is the single source of truth for every path and tunable, and each one
can be overridden by an `INTERNSTASH_*` environment variable — see
[`.env.example`](.env.example). More detail in [`data/README.md`](data/README.md).

---

## Features

### Kanban board
Five active columns — `suggested · applied · OA · interviewing · offer` — plus
`rejected`. Cards show company, role, deadline, and tags.

**`rejected` is on the board, behind a one-click toggle.** It's the outcome of
work you actually did, so it sits next to the rest rather than being filed away
with the noise — but it's a record, not a queue, so the coloured **rejected**
button in the top bar folds the column away when you want the board to be only
what's still live. The button keeps its count either way, so you can see how much
is hidden, and the choice sticks across reloads. Nothing is refetched: those rows
are always loaded, the toggle is pure layout.

`not interested` is the only **archived** status: hidden behind the *Show
archived* toggle in the filter drawer, and left out of every query until you ask
for it.

**`not interested` earns its keep.** Deleting a feed suggestion doesn't make it
go away — the next fetch re-imports it, because deleting the row also deletes the
dedupe key. Marking it *not interested* keeps the row (and its `external_id`) in
the database, which permanently suppresses re-import while getting it off your
board. That's the difference between "gone" and "gone for good".

### Term (season) as its own axis
Every entry carries a `season` — `Summer 2027`, `Fall 2026`, `Spring 2027` — held
in a dedicated column rather than buried in the description text. It's a separate
filter axis from tags, because a term is a *scale*, not a topic: it sorts
chronologically and shouldn't share a list with `pytorch`.

Terms come from the feed's own `season` field, fall back to parsing the job
title, and are extracted by the local model for pasted postings. Input is
normalized, so `summer 2027`, `Summer, 2027`, and `SUMMER 2027` all land on the
same value. `Autumn` folds into `Fall`.

Postings that never state a term are a real third state (51 of 126 feed entries
at time of writing) — stored as `NULL` and filterable as **not stated** rather
than silently dropped.

### Auto-archiving terms you don't want
Set which terms skip triage entirely:

```bash
export INTERNSTASH_AUTO_NOT_INTERESTED_SEASONS="Fall 2026"
```

Matching postings import straight to `not_interested`, with the term recorded in
`notes` so you can see why. Default is `Fall 2026`; set it empty to disable.

**It only ever touches `suggested` rows.** Anything you've already triaged —
applied, interviewing, or filed by hand — is left alone, and a row you
deliberately pull back out of the archive won't be re-archived. The one-time
backfill in migration 3 follows the same rule, and appends to existing notes
rather than overwriting them.

### Filter by tag
A drawer that slides in from the left, **closed by default** — click **☰ Filters**
in the header to open it, `Esc` or click-away to dismiss. While it's shut, the
trigger shows a count badge so an actively-filtered board never looks like a
broken one.

Inside: **Areas** (`swe`, `mle`, `health-tech`, `bio`, `fintech`, `infra`,
`hardware`, `data`) listed first in bold, then incidental **skills** (`python`,
`pytorch`, `sql`) below a divider. Checking several is an OR. Counts show how
many entries carry each tag. The *Show archived* toggle lives here too.

Near-synonyms are merged so they don't compete for the same jobs —
`software`, `software-engineering`, and `software-development` all collapse into
`swe`; the feed's own `Data & ML/AI` category folds into `mle` alongside `ai`,
`llms`, and `computer-vision`. Hover any tag to see what merged into it.

Merging happens at **read time only** — the original tag text stays in the
database. So editing `TAG_SYNONYMS` in [`app/config.py`](app/config.py)
re-groups your existing entries retroactively, and nothing the model wrote is
ever destroyed.

### Save is safe to spam
Clicking **Save** repeatedly creates exactly one entry, guaranteed in two
independent layers:

1. **The form locks while saving.** The button disables and reads "Saving…", and
   a re-entrant submit is ignored outright.
2. **The server enforces it.** Each opened form carries a random
   `idempotency_key`; the column has a `UNIQUE` index, and re-POSTing a key
   returns the row created the first time with `200` instead of `201`.

The second layer is the one that actually holds. A disabled button is a UI
convention, not a constraint — it fails on a JS error, a second tab, or a
browser retry. Verified with 10 concurrent POSTs sharing one key: one `201`,
nine `200`s, exactly one row. The race where two requests both pass the initial
lookup is caught by the unique index and resolved by returning the winner.

Requests that omit a key (curl, scripts) are unaffected and always insert.

### Unsaved work is hard to lose
While an entry has unsaved changes, a stray click on the backdrop **will not
close the dialog** — it shakes and says so instead. Nothing to dismiss, nothing
lost.

Deliberate exits still work: `Esc` and **Cancel** ask before discarding, so
throwing a draft away stays possible. Closing the tab triggers the browser's own
"leave site?" prompt.

Dirtiness is measured by comparing the current form against a snapshot taken
when it opened, so typing a character and deleting it again counts as clean, and
an untouched form always closes freely.

### Frontend changes actually reach the browser
Static assets are served with `Cache-Control: no-cache, must-revalidate`.

Without an explicit policy, browsers fall back to *heuristic* caching, and a
soft reload (F5) can serve `app.js` from memory cache without ever contacting
the server — so a shipped fix silently never lands and every change appears to
"not work". `no-cache` means "always revalidate", not "never store": the ETag
still makes the normal case a `304` with a zero-byte body.

`GET /api/meta` returns a `build` fingerprint of the frontend files, and the page
logs `InternStash frontend build <hash>` to the console on load — so a stale
bundle is identifiable at a glance instead of looking like a logic bug.

### Rich-paste job descriptions
Paste directly from a job posting. The paste handler captures **both** clipboard
flavours from the event — `text/html` (formatting and links, for display) and
`text/plain` (for search) — and stores both.

Formatting, lists, tables, and **links** are preserved. Scripts are not. See
[Sanitization](#how-pasted-html-is-sanitized) below.

### Paste-first entry with local-LLM autofill (optional)

The "New" form is built around one action: **paste the posting, everything else
fills itself in.**

Paste a job description and a local [Ollama](https://ollama.com) model extracts
**company**, **role title**, **deadline**, **source URL**, and **tags** — in about
1–2 seconds once the model is warm. Review, adjust, save.

Ground rules:

- **Local only.** Nothing is sent to any hosted API. These are your job
  applications; they stay on your machine. That's the whole reason for using a
  local model rather than a cloud one.
- **Never overwrites you.** Autofill only populates fields that are *empty*, so
  anything you've typed is safe. Filled fields flash briefly so you know to check
  them.
- **Notes are never touched.** That field is yours.
- **Fully optional.** No Ollama, no problem — the button doesn't render and manual
  entry works exactly as before. Nobody cloning this repo is required to run a model.

Setup, if you want it:

```bash
ollama pull qwen2.5:7b-instruct-q4_K_M
```

Point at a different model with `INTERNSTASH_OLLAMA_MODEL`; any instruct model
that honours structured output will do. Extraction is constrained by a JSON
schema at the decoding level, so the model can't return prose. Values are
re-validated server-side — a URL must really be `http(s)`, a deadline must really
be `YYYY-MM-DD` — so a hallucinated value is dropped rather than stored.

Check status any time at `GET /api/llm/health`.

### Dates: one click for today

`Found`, `Applied`, and `Deadline` each have a **Today** button. New entries
default `Found` to today automatically, since that's when you found it. Clicking
**Today** a second time clears the field, so it doubles as an undo.

### Deadlines stop shouting once you've applied

The ⏳ badge only turns amber (within 7 days) or red (past due) for entries you
still have to act on — `suggested` and `saved`. Once something reaches `applied`,
`oa`, `interviewing`, `offer`, or `rejected`, the deadline is history: the date stays
visible for reference but renders muted, with no hourglass. A board full of red
you can't do anything about is just noise.

### Open the posting in one click

The **↗ Open** button next to `Source URL` opens the posting in a new tab instead
of making you select and copy the field. It's disabled unless the field holds a
real `http(s)` URL, and re-checks at click time so a `javascript:` value typed
into the box can never be executed.

### Full-text search
SQLite FTS5 across job description text, company, role title, and tags. Prefix
matching, so `bio` finds `bioinformatics`. Press `/` to jump to the search box.

### Scheduled feed import
Polls public feeds of Summer 2027 / Fall 2026 tech internships, filters by your
keyword list, and inserts new matches as `suggested` for you to triage. Runs
every 6 hours by default, plus a **Fetch feed** button for on-demand runs.

Sources:
- [zshah101/Automated-List-Of-…-Tech-Internships](https://github.com/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships)
  → `api/jobs.json`
- [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
  → `.github/scripts/listings.json`

Imports are **idempotent**. Each posting carries a stable upstream id (e.g.
`greenhouse:pdtpartners:8077685`, or `vanshb03:<uuid>` for the second source)
stored in a `UNIQUE` column, so re-running never creates duplicates — even for
entries you've since re-triaged or edited.

Two cleanup passes run after every fetch, both scoped to `suggested` rows only
— anything you've already triaged is never touched:
- **Padlock rule.** vanshb03's list marks a posting closed (🔒 in their
  README) with `active: false`. A `suggested` posting that closes after we
  imported it gets removed from triage; one that's already closed when we see
  it is never suggested in the first place.
- **Cross-source dedupe.** If a fresh `suggested` row's `source_url` matches
  the URL on a row you've already moved to another status, the redundant
  suggestion is dropped — same opportunity, already triaged.

#### Tuning the keywords (worth reading)

Default list: `python, machine learning, health, clinical, bio`

Matching is a **case-insensitive substring** test against company + title +
category + skills. Substring rather than word-boundary is deliberate, so `bio`
catches `biotech` / `biomedical` / `bioinformatics`.

**Heads-up on `python`:** the upstream feed tags `python` in the `skills` array of
almost every software role. On a recent snapshot that single keyword matched
**69 of 107** postings, while `bio` and `clinical` matched zero. Left as-is, your
`suggested` column will fill with general SWE roles.

If you want a tighter signal, drop `python` and lean on domain terms:

```bash
export INTERNSTASH_FETCH_KEYWORDS="machine learning,health,clinical,bio,genomic,neuro"
```

Or set it empty to import everything and triage manually.

---

## How pasted HTML is sanitized

Job boards render employer-submitted HTML, so a pasted JD is untrusted input.
All HTML is sanitized **on input** with [nh3](https://pypi.org/project/nh3/) —
only clean markup is ever written to the database.

**Why a library instead of a hand-rolled allowlist:** Python's `html.parser` is a
*tokenizer*; browsers use the HTML5 *tree-construction* algorithm. They disagree
on malformed markup, so a tokenizer-based sanitizer can approve a string whose
browser-built DOM is a different, executable thing — mutation XSS. The classic
case:

```html
<noscript><p title="</noscript><img src=x onerror=alert(1)>"></noscript>
```

`html.parser` reads that as one inert `<p>` with a long title. A browser closes
the `<noscript>` and builds a live `<img onerror>`. nh3 wraps Rust's
`ammonia`/`html5ever` and parses with a real HTML5 tree builder, so what it
inspects is what the browser will construct.

What survives: `a` (with `href`/`title`), headings, `p`, `br`, lists, `strong`,
`em`, `b`, `i`, `u`, `code`, `pre`, `blockquote`, tables, `hr`.
Links get `target="_blank"` and `rel="noopener noreferrer nofollow"`; URL schemes
are limited to `http`, `https`, `mailto` (no `javascript:`, no `data:`).
Everything else is unwrapped to text, and `script`/`style`/`iframe` contents are
discarded entirely.

The browser paste box sends clipboard HTML to `POST /api/sanitize` *before*
inserting it into the live page, because inserting raw clipboard markup is itself
an XSS sink — `<script>` stays inert via `innerHTML`, but inline handlers like
`onerror` still fire. That keeps nh3 as the single sanitizer rather than
duplicating an allowlist in JavaScript.

---

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/applications?q=&status=&tags=&season=&include_archived=` | List / search / tag filter |
| `GET` | `/api/tags` | Canonical tags with counts, for the filter |
| `GET` | `/api/seasons` | Term facets with counts, chronological |
| `POST` | `/api/applications` | Create |
| `GET` | `/api/applications/{id}` | Fetch one |
| `PATCH` | `/api/applications/{id}` | Partial update |
| `DELETE` | `/api/applications/{id}` | Delete |
| `POST` | `/api/fetch/run` | Import from the feed now |
| `GET` | `/api/fetch/status` | Result of the last import |
| `POST` | `/api/sanitize` | Clean an HTML fragment |
| `POST` | `/api/extract` | Pull fields from a pasted JD via local Ollama |
| `GET` | `/api/llm/health` | Is local autofill available, and why not |
| `GET` | `/api/meta` | Statuses, counts, fetch + LLM config, today's date |
| `GET` | `/api/healthz` | Liveness + resolved DB path |

Interactive docs at `/docs` (FastAPI's built-in Swagger UI).

---

## Schema

One table, `applications`:

| Column | Notes |
| --- | --- |
| `id` | integer PK |
| `company`, `role_title` | required |
| `status` | `CHECK`-constrained: `suggested`, `applied`, `oa`, `interviewing`, `offer`, `rejected`, `not_interested` |
| `date_found`, `date_applied`, `deadline` | ISO `YYYY-MM-DD`, all optional |
| `jd_text_plain` | plain text, feeds the search index |
| `jd_html` | sanitized HTML, for display |
| `source_url`, `notes`, `tags` | `tags` is a comma-separated string |
| `season` | canonical term e.g. `Summer 2027`; `NULL` when unstated |
| `external_id` | `UNIQUE`; upstream feed id, `NULL` for manual entries |
| `created_at`, `updated_at` | UTC timestamps |

Plus `applications_fts`, an external-content FTS5 index kept in sync by triggers
(so the text isn't stored twice).

Migrations are plain SQL in [`app/db.py`](app/db.py), versioned with SQLite's
`user_version` pragma and applied automatically at startup. To add one: append to
`MIGRATIONS`, never edit a shipped entry.

---

## Configuration

Every setting is an environment variable with a working default. Full list with
comments in [`.env.example`](.env.example); defaults in [`app/config.py`](app/config.py).

| Variable | Default | Purpose |
| --- | --- | --- |
| `INTERNSTASH_DATA_DIR` | `./data` | Where all runtime data lives |
| `INTERNSTASH_DB_PATH` | `<data>/internstash.db` | SQLite file |
| `INTERNSTASH_HOST` | `0.0.0.0` | Bind address |
| `INTERNSTASH_PORT` | `1337` | Port |
| `INTERNSTASH_FETCH_INTERVAL_MINUTES` | `360` | Poll interval; `0` disables |
| `INTERNSTASH_FETCH_ON_STARTUP` | `1` | Import shortly after boot |
| `INTERNSTASH_FETCH_KEYWORDS` | see above | Comma-separated filter |
| `INTERNSTASH_FETCH_EXCLUDE_KEYWORDS` | `phd` | Comma-separated title exclude filter |
| `INTERNSTASH_FEED_URL` | upstream `jobs.json` | zshah101 feed source |
| `INTERNSTASH_FEED_URL_VANSHB03` | upstream `listings.json` | vanshb03 feed source |
| `INTERNSTASH_OLLAMA_ENABLED` | `1` | `0` disables LLM autofill |
| `INTERNSTASH_OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `INTERNSTASH_OLLAMA_MODEL` | `qwen2.5:7b-instruct-q4_K_M` | Extraction model |
| `INTERNSTASH_AUTO_NOT_INTERESTED_SEASONS` | `Fall 2026` | Terms that skip triage |

---

## Running it as a service (autostart at boot)

A **systemd user service** is the recommended setup — no root required, and the
unit lives in your home directory alongside the app.

Create `~/.config/systemd/user/internstash.service`:

```ini
[Unit]
Description=InternStash — self-hosted internship application tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/internstash
ExecStart=/path/to/internstash/.venv/bin/python run.py
Environment=PYTHONUNBUFFERED=1
# Override config here instead of editing the repo, e.g.:
#   Environment=INTERNSTASH_PORT=1738
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
```

Enable and start it:

```bash
systemctl --user daemon-reload && systemctl --user enable --now internstash
```

**Then enable lingering**, or the service will only start when you log in rather
than at boot — the part that's easy to miss on a headless box:

```bash
loginctl enable-linger $USER
```

Check it took with `loginctl show-user $USER -p Linger` (want `Linger=yes`). On
most desktop distros polkit permits this without `sudo`; if it refuses, prefix
with `sudo`.

Everyday commands:

```bash
systemctl --user status internstash      # is it up?
systemctl --user restart internstash     # after editing code
journalctl --user -u internstash -f      # live logs
```

The scheduler runs inside the app, so no cron entry is needed. If you'd rather
drive imports from cron, set `INTERNSTASH_FETCH_INTERVAL_MINUTES=0` and call
`curl -X POST http://localhost:1337/api/fetch/run` on your own schedule.

> Prefer a system-wide unit at `/etc/systemd/system/`? That works too — add
> `User=youruser`, use `WantedBy=multi-user.target`, drop the `--user` flags, and
> skip lingering entirely. It just needs root.

---

## Persistence, reboots, and other devices

**Your data is a file on disk, not browser state.** Nothing lives in
localStorage or in a tab. Every save is an immediate write to
`data/internstash.db`.

| Scenario | What happens |
| --- | --- |
| Reboot the host | Data intact. The systemd unit restarts the app automatically (lingering enabled — see above). |
| Power loss | Committed saves survive. SQLite runs in WAL mode with `synchronous=FULL`, so every commit is `fsync`ed to disk before it returns. |
| App crashes | systemd restarts it (`Restart=on-failure`). Verified by `SIGKILL`ing the process mid-session: the row was still there and `PRAGMA integrity_check` returned `ok`. |
| Open on your phone / laptop | Same data. It's a server, not a local app — every device talks to the one database on the host. |
| Close the browser | Nothing to lose; the save already happened. |

**Two honest caveats:**

1. **Consumer SSDs sometimes lie about `fsync`.** If the drive acknowledges a
   flush before the data is really on physical media, a hard power cut can still
   lose the last transaction or two. That's a hardware property, not something
   the app can fix. The database won't corrupt either way — WAL is
   crash-safe by design.
2. **No live sync between devices.** Two browsers open at once won't see each
   other's edits until refreshed, and simultaneous edits to the *same* entry are
   last-write-wins. Fine for one person on a few devices; not a collaboration tool.

The only thing not covered is disk failure or an accidental `rm`. See Backups.

## Backups

It's one SQLite file. Copy it while the app is stopped, or use the safe backup
command any time:

```bash
sqlite3 data/internstash.db ".backup 'internstash-backup.db'"
```

---

## Not in v1

- **Multi-user support** — explicitly out of scope; this is single-tenant by design
- Authentication (see the security note above)
- Drag-and-drop between columns (change status in the edit dialog)
- Email/calendar reminders for deadlines
- Resume or cover-letter attachments

---

## License

MIT — do what you like. If you fork this, double-check `data/` is still ignored
before your first push.
