from __future__ import annotations

import json
import fnmatch
import shutil
import subprocess
import tempfile
import time
import re
from pathlib import Path

from .models import ArtifactBundle, BenchmarkInstance, Prediction, TokenUsage


class AgentGenerationError(RuntimeError):
    pass


class AgentGenerator:
    """Generate an SDD artifact bundle and code patch from a public Instance.

    The generator receives no EvaluationOracle, so hidden tests and the gold
    patch cannot leak into the coding-agent prompt.
    """

    def __init__(self, runner=subprocess.run):
        self.runner = runner

    @staticmethod
    def _command(name: str) -> str:
        return shutil.which(name) or shutil.which(f"{name}.cmd") or name

    def _run(self, command: list[str], cwd: Path, timeout: int = 1200):
        try:
            return self.runner(
                command, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentGenerationError(str(error)) from error

    def _checkout(self, instance: BenchmarkInstance, destination: Path) -> Path:
        git = self._command("git")
        commands = [
            ([git, "init", str(destination)], destination.parent, 60, "repository initialization"),
            ([git, "-C", str(destination), "remote", "add", "origin", instance.repo], destination.parent, 30, "remote configuration"),
            ([git, "-C", str(destination), "-c", "protocol.version=2", "fetch", "--depth=1", "--no-tags", "origin", instance.base_commit], destination.parent, 600, "base commit fetch"),
            ([git, "-C", str(destination), "-c", "advice.detachedHead=false", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false", "checkout", "--detach", "FETCH_HEAD"], destination.parent, 300, "base commit checkout"),
        ]
        for command, cwd, timeout, label in commands:
            result = self._run(command, cwd, timeout)
            if result.returncode:
                raise AgentGenerationError(f"{label} failed: {(result.stdout + result.stderr)[-2000:]}")
        return destination.resolve()

    @staticmethod
    def _prompt(instance: BenchmarkInstance, workflow: str) -> str:
        requirements = "\n".join(
            f"- {item.id} [{item.priority}/{item.kind}]: {item.description}\n  Acceptance: {'; '.join(item.acceptance_criteria) or 'See problem statement'}"
            for item in instance.requirements
        ) or "- Derive requirements from the problem statement."
        constraints = "\n".join(f"- {item}" for item in instance.constraints) or "- Preserve existing behavior and public APIs."
        if workflow == "openspec":
            process = "Use the installed OpenSpec workflow. Create proposal.md, design.md, and tasks.md under openspec/, then implement every task."
        elif workflow == "codespec":
            process = "Use the CodeSpec specification workflow. Create spec.md, design.md, and tasks.md under codespec/, then implement every task."
        else:
            process = "Use a Superpowers spec-plan-implement-test workflow. Create spec.md, plan.md, and tasks.md under superpowers/, then implement every task."
        return f"""Implement this benchmark Instance end to end using specification-driven development.

Instance: {instance.instance_id}
Problem:
{instance.problem_statement}

Requirements:
{requirements}

Constraints:
{constraints}

Repository scope:
The implementation working directory is `{instance.environment.working_directory}` relative to the repository root. Make all production-code and test changes inside that directory. Do not edit sibling tutorial stages such as `initial/` when the working directory is `complete/`.

Workflow:
{process}

Design review contract:
The design document must trace every requirement to exact implementation files/symbols and verification tests. Explicitly assess high availability (failover, degradation, recovery targets), high concurrency (capacity, limits, idempotency, backpressure, and race safety), dependency failures, observability, rollback, and testability. Include a Mermaid or equivalent flowchart with entry, decision/branch, success, and failure paths. For Java changes, run or document the Alibaba Java Coding Guidelines (P3C) result. Keep the design and the generated source patch consistent; do not claim behavior or files that the patch does not implement.

Inspect the repository before editing. Make the smallest production-quality change that satisfies the requirements. Repository-owned tests may be changed when the public requirement explicitly concerns them. Never create, modify, search for, or infer hidden evaluator tests under `.sdd_eval_tests`; the benchmark applies those private tests only after generation. Do not access paths outside this workspace. Run the configured build and existing tests when practical. Finish with actual repository changes, not only documentation."""

    @staticmethod
    def _change_name(instance_id: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", instance_id.lower()).strip("-")
        return f"benchmark-{slug or 'instance'}"

    def _prepare_workflow(self, root: Path, instance: BenchmarkInstance, client: str, workflow: str) -> list[str]:
        logs: list[str] = []
        if workflow == "openspec":
            executable = self._command("openspec")
            initialized = self._run([executable, "init", str(root), "--tools", client], root, 180)
            logs.append((initialized.stdout or "") + (initialized.stderr or ""))
            if initialized.returncode:
                raise AgentGenerationError(f"OpenSpec initialization failed: {logs[-1][-2000:]}")
            change = self._change_name(instance.instance_id)
            created = self._run([executable, "new", "change", change, "--description", instance.problem_statement[:200]], root, 180)
            logs.append((created.stdout or "") + (created.stderr or ""))
            if created.returncode:
                raise AgentGenerationError(f"OpenSpec change creation failed: {logs[-1][-2000:]}")
        elif workflow == "codespec":
            (root / "codespec").mkdir(exist_ok=True)
        else:
            (root / "superpowers").mkdir(exist_ok=True)
        return logs

    def _agent_command(self, root: Path, client: str, model: str, prompt: str) -> list[str]:
        if client == "codex":
            return [
                self._command("codex"), "--profile", "relay", "exec", "--cd", str(root),
                "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                "--json", "--model", model, prompt,
            ]
        model = self._opencode_model(model)
        return [
            self._command("opencode"), "run", "--dir", str(root),
            "--format", "json", "--auto", "--model", model, prompt,
        ]

    @staticmethod
    def _token_usage(client: str, model: str, output: str, latency_ms: int) -> TokenUsage:
        """Extract usage from Codex/OpenCode JSONL without counting display events twice."""
        input_tokens = output_tokens = 0
        parsed = False
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if client == "opencode" and event.get("type") == "step_finish":
                tokens = (event.get("part") or {}).get("tokens") or {}
                cache = tokens.get("cache") or {}
                input_tokens += int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(cache.get("write") or 0)
                output_tokens += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
                parsed = True
            elif client == "codex" and event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                # Codex reports aggregate usage for the completed turn. Keep the
                # cached portion inside input_tokens because it is consumed input.
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
                parsed = True
        if not parsed and client == "codex":
            # Compatibility with older non-JSON Codex output. It exposes only a
            # total, so retain it as estimated input rather than losing usage.
            matches = re.findall(r"(?im)^tokens used\s*\r?\n\s*([\d,]+)\s*$", output)
            if matches:
                input_tokens = int(matches[-1].replace(",", ""))
                parsed = True
        return TokenUsage(
            input_tokens=input_tokens, output_tokens=output_tokens,
            provider=f"{client}:{model}", mode="model" if output_tokens else "total-only",
            estimated=not parsed or (client == "codex" and output_tokens == 0), latency_ms=latency_ms,
        )

    @staticmethod
    def _opencode_model(model: str) -> str:
        """Resolve friendly aliases to provider-qualified OpenCode model IDs."""
        normalized = re.sub(r"[^a-z0-9]+", "", model.lower())
        return {
            "glm53": "gateway/glm-5.3",
            "glm53flash": "gateway/glm-5.3-flash",
            "minimax27": "gateway/minimax-2.7",
        }.get(normalized, model)

    @staticmethod
    def _temporary_opencode_config(root: Path, model: str):
        """Declare gateway models that work upstream but are absent from the local catalog."""
        if model != "gateway/minimax-2.7":
            return None
        path = root / "opencode.json"
        original = path.read_bytes() if path.exists() else None
        try:
            payload = json.loads(original.decode("utf-8")) if original else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        provider = payload.setdefault("provider", {}).setdefault("gateway", {})
        provider.setdefault("models", {})["minimax-2.7"] = {
            "name": "MiniMax 2.7", "tool_call": True,
            "limit": {"context": 128000, "output": 8192},
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path, original

    @staticmethod
    def _restore_opencode_config(state) -> None:
        if not state:
            return
        path, original = state
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)

    def _documents(self, root: Path, agent_root: Path, workflow: str) -> dict[str, str]:
        root = root.resolve()
        agent_root = agent_root.resolve()
        base = agent_root / ("openspec" if workflow == "openspec" else "codespec" if workflow == "codespec" else "superpowers")
        documents = {}
        if base.exists():
            for path in base.rglob("*.md"):
                documents[str(path.relative_to(root)).replace("\\", "/")] = path.read_text(encoding="utf-8", errors="replace")
        return documents

    def _ensure_documents(self, agent_root: Path, instance: BenchmarkInstance, workflow: str) -> None:
        requirements = "\n".join(f"- {item.id}: {item.description}" for item in instance.requirements) or f"- {instance.problem_statement}"
        if workflow == "openspec":
            base = agent_root / "openspec" / "changes" / self._change_name(instance.instance_id)
            templates = {
                "proposal.md": f"# Proposal\n\n## Problem\n{instance.problem_statement}\n\n## Requirements\n{requirements}\n",
                "design.md": f"# Design\n\nImplement the smallest compatible change inside `{instance.environment.working_directory}`.\n\n## Requirements\n{requirements}\n\n## Implementation and Traceability\n- Files and symbols: TODO\n- Verification: TODO\n\n## Availability and Recovery\nState failover, degradation, timeout/retry behavior, and SLO/RTO/RPO targets, or record why this is not applicable.\n\n## Concurrency and Capacity\nState QPS/TPS or other bounds, synchronization/idempotency, rate limits, and backpressure, or record why this is not applicable.\n\n## Failure Handling and Observability\nDescribe error paths, rollback/compensation, metrics, logs, alerts, and test or load-test oracles.\n\n## Flowchart\n```mermaid\nflowchart TD\n    Start[Request] --> Decision{{Validate}}\n    Decision -->|success| Done[Success]\n    Decision -->|failure| Error[Error or fallback]\n```\n",
                "tasks.md": "# Tasks\n\n" + "\n".join(f"- [x] Implement {item.id}: {item.description}" for item in instance.requirements) + "\n",
            }
        elif workflow == "codespec":
            base = agent_root / "codespec"
            templates = {
                "spec.md": f"# CodeSpec\n\n## Problem\n{instance.problem_statement}\n\n## Requirements\n{requirements}\n",
                "design.md": f"# Design\n\nImplement the requirements inside `{instance.environment.working_directory}`.\n\n## Traceability\n{requirements}\n",
                "tasks.md": "# Tasks\n\n" + "\n".join(f"- [ ] Implement {item.id}: {item.description}" for item in instance.requirements) + "\n",
            }
        else:
            base = agent_root / "superpowers"
            templates = {
                "spec.md": f"# Specification\n\n{instance.problem_statement}\n\n## Requirements\n{requirements}\n",
                "plan.md": f"# Plan\n\nImplement and verify the required source-code change inside `{instance.environment.working_directory}`.\n\n## Availability and Recovery\nState failover, degradation, timeout/retry behavior, and SLO/RTO/RPO targets, or record why this is not applicable.\n\n## Concurrency and Capacity\nState QPS/TPS or other bounds, synchronization/idempotency, rate limits, and backpressure, or record why this is not applicable.\n\n## Failure Handling and Observability\nDescribe error paths, rollback/compensation, metrics, logs, alerts, and test or load-test oracles.\n\n## Flowchart\n```mermaid\nflowchart TD\n    Start[Request] --> Decision{{Validate}}\n    Decision -->|success| Done[Success]\n    Decision -->|failure| Error[Error or fallback]\n```\n",
                "tasks.md": "# Tasks\n\n" + "\n".join(f"- [x] {item.description}" for item in instance.requirements) + "\n",
            }
        base.mkdir(parents=True, exist_ok=True)
        for name, content in templates.items():
            path = base / name
            if not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip():
                path.write_text(content, encoding="utf-8")

    def _patch(self, root: Path, instance: BenchmarkInstance) -> str:
        scope = (instance.environment.working_directory or ".").replace("\\", "/").rstrip("/") or "."
        prefix = "" if scope == "." else f"{scope}/"
        self._run([self._command("git"), "add", "-N", "--", scope], root, 60)
        result = self._run([
            self._command("git"), "diff", "--binary", "--", scope,
            f":(exclude){prefix}openspec/**", f":(exclude){prefix}codespec/**", f":(exclude){prefix}superpowers/**",
            f":(exclude){prefix}.codex/**", f":(exclude){prefix}.opencode/**",
            f":(exclude){prefix}.sdd_eval_tests/**",
        ], root, 120)
        if result.returncode:
            raise AgentGenerationError(f"could not capture generated patch: {(result.stdout + result.stderr)[-2000:]}")
        patch = self._filter_patch(result.stdout, scope)
        if not patch.strip():
            raise AgentGenerationError("coding agent completed without generating a source-code patch")
        return patch

    @staticmethod
    def _excluded_patch_path(path: str, forbidden_paths: list[str] | None = None) -> bool:
        """Keep generated predictions focused on production source files.

        Git pathspec exclusions are version/configuration sensitive, so apply
        the same policy to captured diff sections as a final, deterministic
        guard. This also prevents agent-installed skills and workflow artifacts
        from being evaluated as code changes.
        """
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        parts = normalized.split("/")
        lowered = normalized.lower()
        if any(part in {".codex", ".opencode", "openspec", "codespec", "superpowers", ".sdd_eval_tests"} for part in parts):
            return True
        if any(fnmatch.fnmatch(normalized, pattern) for pattern in (forbidden_paths or [])):
            return True
        return False

    @classmethod
    def _filter_patch(cls, patch: str, scope: str = ".", forbidden_paths: list[str] | None = None) -> str:
        """Remove excluded files while preserving complete unified-diff sections."""
        sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
        kept: list[str] = []
        for section in sections:
            if not section.strip():
                continue
            header = next((line for line in section.splitlines() if line.startswith("diff --git ")), "")
            match = re.match(r"diff --git a/(.+) b/(.+)$", header)
            if match and not cls._excluded_patch_path(match.group(2), forbidden_paths):
                kept.append(section)
        return "".join(kept)

    def generate(self, instance: BenchmarkInstance, client: str, model: str, workflow: str, workspace: str | None = None) -> Prediction:
        parent = Path(workspace) if workspace else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        # OpenCode may leave a short-lived child process holding a repository
        # file on Windows after it has returned.  Cleanup must not turn an
        # otherwise valid prediction into a generation failure; the OS will
        # release the handle and the directory can be removed later.
        with tempfile.TemporaryDirectory(
            prefix="sdd-agent-", dir=parent, ignore_cleanup_errors=True,
        ) as temporary:
            root = self._checkout(instance, Path(temporary) / "repo")
            agent_root = (root / instance.environment.working_directory).resolve()
            if agent_root != root.resolve() and root.resolve() not in agent_root.parents:
                raise AgentGenerationError("working_directory escapes the repository")
            if not agent_root.is_dir():
                raise AgentGenerationError(f"working_directory does not exist: {instance.environment.working_directory}")
            workflow_logs = self._prepare_workflow(agent_root, instance, client, workflow)
            prompt = self._prompt(instance, workflow)
            # Large framework repositories routinely require more than twenty
            # minutes for inspection, implementation, and local verification.
            # Keep clone/workflow/test limits separate, but give the coding
            # agent enough time to complete an end-to-end change.
            resolved_model = self._opencode_model(model) if client == "opencode" else model
            config_state = self._temporary_opencode_config(agent_root, resolved_model) if client == "opencode" else None
            try:
                result = self._run(
                    self._agent_command(agent_root, client, model, prompt),
                    agent_root,
                    # Large repositories can spend over an hour in agent
                    # inspection and verification; keep the worker lease
                    # independent so heartbeats continue during the run.
                    timeout=7200,
                )
            finally:
                self._restore_opencode_config(config_state)
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode:
                raise AgentGenerationError(f"{client} generation failed for {resolved_model} ({result.returncode}): {output[-4000:]}")
            self._ensure_documents(agent_root, instance, workflow)
            patch = self._patch(root, instance)
            documents = self._documents(root, agent_root, workflow)
        return Prediction(
            instance_id=instance.instance_id,
            model_name_or_path=self._opencode_model(model) if client == "opencode" else model,
            client=client,
            workflow=workflow,
            model_patch=patch,
            artifacts=ArtifactBundle(
                documents=documents,
                logs={"workflow": "\n".join(workflow_logs)[-8000:], "agent": output[-12000:]},
            ),
            token_usage=self._token_usage(
                client, model, output, int((time.perf_counter() - started) * 1000),
            ),
        )
