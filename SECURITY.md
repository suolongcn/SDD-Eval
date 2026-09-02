# Security Policy

Do not open a public issue for a suspected vulnerability. Contact the maintainer privately through the GitHub security contact for `suolongcn/SDD-Eval` with reproduction steps, impact, and the affected version.

Never commit keys, `sdd_eval.db`, private Oracle exports, generated repositories, or prompts containing private data.

The evaluator executes configured setup, build, and test commands in disposable workspaces. Treat repositories and model-generated patches as untrusted input. Use Docker Backend with least privilege and grading network disabled; Local Backend is for trusted development only.
