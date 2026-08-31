# SDD Eval

SDD Eval is a lightweight evaluation workspace for spec-driven development tools. It turns issue-backed engineering tasks into repeatable runs with archived specifications, generated code, build/test evidence, and scoring.

## Features

- FastAPI service and browser dashboard for tasks and runs.
- OpenSpec and Superpowers workflow adapters.
- Codex CLI, OpenCode CLI, mock, and OpenAI-compatible model providers.
- GitHub, Gitee, and GitCode project metadata with issue/PR references.
- SQLite persistence with explicit run artifacts and failure records.

## Quick start

Requirements: Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pytest
python -m sdd_eval.cli serve
```

Open `http://127.0.0.1:8000`, then import sample tasks:

```powershell
python -m sdd_eval.cli import-task examples/tasks/issue-backed-open-source.json
```

Run a task from the dashboard or CLI:

```powershell
python -m sdd_eval.cli run petclinic-issue-2600 --client codex --model gpt-5.6-luna --tool openspec
```

Use `--model mock` for an offline pipeline smoke test. Mock runs intentionally receive no quality credit because they do not generate an implementation.

## Configuration

For an OpenAI-compatible HTTP endpoint, pass its URL as the model selector and set `SDD_EVAL_MODEL` plus `SDD_EVAL_API_KEY`. Transient gateway failures are retried; when a real provider fails, the OpenSpec adapter falls back to the workspace-aware Codex CLI when available. Set `SDD_EVAL_PROVIDER_RETRIES` to change the retry count (default: 3).

Never commit API keys, `sdd_eval.db`, or `.sdd-runs`; these are ignored by Git.

## Project map

| Path | Purpose |
| --- | --- |
| `sdd_eval/` | Application, providers, adapters, evaluator, and storage |
| `tests/` | Fast unit and smoke tests |
| `examples/tasks/` | Issue-backed task catalog |
| `.github/` | CI, contribution templates, and repository automation |
| `DEVELOPMENT.md` | Local development and architecture guide |

## Documentation

- [Development guide](DEVELOPMENT.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## License

This repository is currently distributed without a declared open-source license. Add a license before publishing releases or accepting broad external contributions.
