from __future__ import annotations

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
        clone_command = [self._command("git"), "clone", "--no-hardlinks"]
        if instance.repo.startswith(("http://", "https://")):
            # GitHub-sized repositories are more reliable through the proxy when
            # blobs and tags are fetched lazily; checkout below still materializes
            # the requested base commit.
            clone_command.extend(["--filter=blob:none", "--no-tags", "--no-checkout"])
        clone_command.extend([instance.repo, str(destination)])
        clone = self._run(clone_command, destination.parent, 600)
        if clone.returncode:
            raise AgentGenerationError(f"repository clone failed: {(clone.stdout + clone.stderr)[-2000:]}")
        checkout = self._run([self._command("git"), "checkout", "--detach", instance.base_commit], destination, 180)
        if checkout.returncode:
            raise AgentGenerationError(f"base commit checkout failed: {(checkout.stdout + checkout.stderr)[-2000:]}")
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

Inspect the repository before editing. Make the smallest production-quality source-code change that satisfies the requirements. Do not create or modify test files; the benchmark's private executable oracle supplies authoritative tests. Do not invent or search for hidden evaluator tests. Do not access paths outside this workspace. Run the configured build and existing tests when practical. Finish with actual source-code changes, not only documentation."""

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
        else:
            (root / "superpowers").mkdir(exist_ok=True)
        return logs

    def _agent_command(self, root: Path, client: str, model: str, prompt: str) -> list[str]:
        if client == "codex":
            return [
                self._command("codex"), "--profile", "relay", "exec", "--cd", str(root),
                "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                "--model", model, prompt,
            ]
        return [
            self._command("opencode"), "run", "--dir", str(root),
            "--format", "json", "--auto", "--model", model, prompt,
        ]

    def _documents(self, root: Path, agent_root: Path, workflow: str) -> dict[str, str]:
        root = root.resolve()
        agent_root = agent_root.resolve()
        base = agent_root / ("openspec" if workflow == "openspec" else "superpowers")
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
                "design.md": f"# Design\n\nImplement the smallest compatible change inside `{instance.environment.working_directory}`.\n\n## Requirements\n{requirements}\n",
                "tasks.md": "# Tasks\n\n" + "\n".join(f"- [x] Implement {item.id}: {item.description}" for item in instance.requirements) + "\n",
            }
        else:
            base = agent_root / "superpowers"
            templates = {
                "spec.md": f"# Specification\n\n{instance.problem_statement}\n\n## Requirements\n{requirements}\n",
                "plan.md": f"# Plan\n\nImplement and verify the required source-code change inside `{instance.environment.working_directory}`.\n",
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
            f":(exclude){prefix}openspec/**", f":(exclude){prefix}superpowers/**",
            f":(exclude){prefix}.codex/**", f":(exclude){prefix}.opencode/**",
            f":(exclude){prefix}src/test/**", f":(exclude){prefix}tests/**",
            f":(exclude){prefix}test/**", f":(exclude){prefix}__tests__/**",
        ], root, 120)
        if result.returncode:
            raise AgentGenerationError(f"could not capture generated patch: {(result.stdout + result.stderr)[-2000:]}")
        patch = self._filter_patch(result.stdout, scope)
        if not patch.strip():
            raise AgentGenerationError("coding agent completed without generating a source-code patch")
        return patch

    @staticmethod
    def _excluded_patch_path(path: str) -> bool:
        """Keep generated predictions focused on production source files.

        Git pathspec exclusions are version/configuration sensitive, so apply
        the same policy to captured diff sections as a final, deterministic
        guard. This also prevents agent-installed skills and workflow artifacts
        from being evaluated as code changes.
        """
        normalized = path.replace("\\", "/").lstrip("./")
        parts = normalized.split("/")
        lowered = normalized.lower()
        if any(part in {".codex", ".opencode", "openspec", "superpowers", "tests", "test", "__tests__"} for part in parts):
            return True
        return any(part.lower() == "src" and index + 1 < len(parts) and parts[index + 1].lower() in {"test", "tests"}
                   for index, part in enumerate(parts))

    @classmethod
    def _filter_patch(cls, patch: str, scope: str = ".") -> str:
        """Remove excluded files while preserving complete unified-diff sections."""
        sections = re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)
        kept: list[str] = []
        for section in sections:
            if not section.strip():
                continue
            header = next((line for line in section.splitlines() if line.startswith("diff --git ")), "")
            match = re.match(r"diff --git a/(.+) b/(.+)$", header)
            if match and not cls._excluded_patch_path(match.group(2)):
                kept.append(section)
        return "".join(kept)

    def generate(self, instance: BenchmarkInstance, client: str, model: str, workflow: str, workspace: str | None = None) -> Prediction:
        parent = Path(workspace) if workspace else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="sdd-agent-", dir=parent) as temporary:
            root = self._checkout(instance, Path(temporary) / "repo")
            agent_root = (root / instance.environment.working_directory).resolve()
            if agent_root != root.resolve() and root.resolve() not in agent_root.parents:
                raise AgentGenerationError("working_directory escapes the repository")
            if not agent_root.is_dir():
                raise AgentGenerationError(f"working_directory does not exist: {instance.environment.working_directory}")
            workflow_logs = self._prepare_workflow(agent_root, instance, client, workflow)
            prompt = self._prompt(instance, workflow)
            result = self._run(self._agent_command(agent_root, client, model, prompt), agent_root)
            output = (result.stdout or "") + (result.stderr or "")
            if result.returncode:
                raise AgentGenerationError(f"{client} generation failed ({result.returncode}): {output[-4000:]}")
            self._ensure_documents(agent_root, instance, workflow)
            patch = self._patch(root, instance)
            documents = self._documents(root, agent_root, workflow)
        return Prediction(
            instance_id=instance.instance_id,
            model_name_or_path=model,
            client=client,
            workflow=workflow,
            model_patch=patch,
            artifacts=ArtifactBundle(
                documents=documents,
                logs={"workflow": "\n".join(workflow_logs)[-8000:], "agent": output[-12000:]},
            ),
            token_usage=TokenUsage(
                provider=f"{client}:{model}", mode="model", estimated=True,
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
        )
