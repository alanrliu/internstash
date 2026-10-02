# `data/` — your personal data lives here

This folder holds **everything InternStash writes at runtime**:

| File | What it is |
| --- | --- |
| `internstash.db` | Your SQLite database — every application, job description, and note |
| `internstash.db-wal`, `internstash.db-shm` | SQLite write-ahead log sidecars |
| `internstash.log` | Log output, if you enable file logging |

## This folder is gitignored on purpose

Your application history is personal: companies you're applying to, notes about
interviews, salary figures, referral contacts. None of that belongs in a public
repository.

The root [`.gitignore`](../.gitignore) ignores everything in here **except**
`.gitkeep` and this README, so a fresh clone gets the folder structure and no
data:

```gitignore
data/*
!data/.gitkeep
!data/README.md
```

> The pattern is `data/*`, not a bare `data/`. Git does not descend into an
> excluded directory, so `data/` would make those `!` re-includes unreachable
> and the folder would disappear for anyone cloning.

## Verify it's working

From the repo root:

```bash
git check-ignore -v data/internstash.db
```

That should print the matching `.gitignore` rule. If it prints nothing, your
database is **not** ignored — stop and fix that before committing.

To double-check nothing personal is staged:

```bash
git status --porcelain data/
```

Only `.gitkeep` and `README.md` should ever appear.

## Backups

Nothing here is backed up for you. It's a single SQLite file, so a copy is
enough — but copy it while the app is stopped, or use SQLite's safe backup:

```bash
sqlite3 data/internstash.db ".backup 'data-backup.db'"
```

## Moving it elsewhere

Set `INTERNSTASH_DATA_DIR` (and optionally `INTERNSTASH_DB_PATH`). Both are
read in [`app/config.py`](../app/config.py), which is the single place any path
is defined — no user-specific path is hardcoded anywhere else in the codebase.
