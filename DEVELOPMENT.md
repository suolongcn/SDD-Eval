# Development Guide

## Environment

- Python 3.11 or newer
- Git
- Optional Codex CLI or OpenCode CLI for real generation runs
- Optional Java/Maven or Node tooling for target repositories

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

## Quality gates

```powershell
python -m pytest -q
python -m compileall -q sdd_eval tests
```

These are the same gates used by GitHub Actions. Keep runtime dependencies small and avoid unrelated formatting churn.

## Service

```powershell
python -m sdd_eval.cli serve --host 127.0.0.1 --port 8000
```

The dashboard is at `/`, API routes are under `/api`, and FastAPI documentation is at `/docs`.

## Architecture

`models.py` defines contracts; `storage.py` persists SQLite state; `api.py` exposes HTTP routes; `adapters.py` runs OpenSpec/Superpowers; `providers.py` handles model calls and retries; `evaluator.py` manages disposable checkouts, validation, scoring, and artifact archival.

Keep provider-specific behavior in `providers.py`. Adapters should return documents, generated paths, token usage, and generation metadata. Failure paths must return a queryable run with explicit artifacts.

## Task authoring

Task files should include a stable ID, repository/revision, concrete requirements, acceptance scenarios, build/test commands, and an issue reference. Record merged PR and commit metadata when available. Do not commit API keys, databases, run workspaces, or generated caches.

## Pull requests

Add focused regression tests, update public documentation for behavior/configuration changes, run both quality gates, and describe compatibility, migration, security, and operational impact.
