# Security Policy

Do not open a public issue for a suspected vulnerability. Contact the maintainer privately through the GitHub security contact for `suolongcn/SDD-Eval` with reproduction steps, impact, and the affected version.

Supply API keys through environment variables or the local credential store. Never commit keys, `sdd_eval.db`, `.sdd-runs`, generated repositories, or prompts containing private data.

The evaluator executes configured build and test commands in disposable workspaces. Treat task repositories and model-generated code as untrusted input and run the service with the least privileges practical.
