---
description: Pre-PR checks — lint changed files, full tests, secrets, docs
---

Run the pre-PR gate for the current working tree. Do not commit unless asked.

1. **Scope** — `git status --short` and `git diff --stat` vs `origin/master`.
   List changed files grouped by area.
2. **Lint only what changed** (the repo has ~300 pre-existing E402 by design):
   ```
   git diff --name-only origin/master -- '*.py' | xargs .venv/bin/ruff check --ignore E402
   ```
   Fix only issues introduced by this change; never reformat untouched files.
3. **Tests** — `make test-fast` (added tests must cover every behaviour
   change). Report the count.
4. **Secrets** — confirm no `.env`, `*.pem`, keys or tokens are staged.
   `git diff --cached --name-only | grep -Ei '\.env$|\.pem$|\.key$'` must be empty.
5. **Artifacts** — nothing under `data/`, `logs/`, `backups/` is staged.
6. **Docs** — if behaviour, Redis fields, configs or service units changed,
   confirm the matching doc/skill was updated (options-data-contract, README,
   design docs, CLAUDE.md).
7. **Summary** — one paragraph: what changed, tests, risks, and anything
   intentionally not done.

$ARGUMENTS
