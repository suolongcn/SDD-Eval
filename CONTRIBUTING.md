# Contributing

Small, focused pull requests are easiest to review.

1. Open or select an issue describing the change.
2. Create a branch from `main`.
3. Implement the smallest coherent change and add tests.
4. Run `python -m pytest -q` and `python -m compileall -q sdd_eval tests`.
5. Open a pull request using the repository template.

Use clear Python and existing project patterns. Preserve API contracts and keep failure records queryable. Commit subjects should be imperative and under 72 characters, for example `Add retry handling for provider disconnects`.
