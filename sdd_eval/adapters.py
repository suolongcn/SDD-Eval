import json
import re
import subprocess

from .providers import provider_for
from .models import TokenUsage


class ToolAdapter:
    def run(self, task, workspace, model):
        workspace = workspace.resolve()
        raise NotImplementedError


def _extract_json(text: str):
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I | re.S).strip()
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(candidate[start : end + 1])
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None
    return None


class OpenSpecAdapter(ToolAdapter):
    def run(self, task, workspace, model):
        workspace = workspace.resolve()
        requirements = "\n".join(f"- {r.id}: {r.description}" for r in task.requirements) or "- No requirements supplied"
        scenarios = "\n".join(f"- {s.id}: Given {s.given}; when {s.when}; then {s.then}" for s in task.acceptance_scenarios) or "- No acceptance scenarios supplied"
        prompt = f"""You are an SDD implementation agent. Return ONLY valid JSON with these keys:
proposal (markdown string), design (markdown string), tasks (markdown string), files (object mapping repository-relative paths to complete file contents).
Task: {task.title}
Requirements:
{requirements}
Acceptance scenarios:
{scenarios}
Constraints:
{chr(10).join('- ' + c for c in task.constraints) or '- None'}
The files object must contain the actual implementation and tests needed for the task. Do not return placeholders."""
        provider = provider_for(model)
        if model.startswith("codex"):
            return self._run_cli(task, workspace, model, "codex")
        if model.startswith("opencode"):
            return self._run_cli(task, workspace, model, "opencode")
        try:
            text, usage = provider.complete(prompt)
        except Exception as provider_error:
            # A proxy disconnect should not discard the run when the
            # workspace-aware CLI is available.  The CLI can continue the
            # workflow while inspecting the checked-out repository directly.
            if provider.simulation:
                raise
            try:
                spec_dir, usage, produced, generation = self._run_cli(task, workspace, "codex", "codex")
                generation["provider_error"] = f"{type(provider_error).__name__}: {provider_error}"
                generation["fallback"] = "codex-cli"
                return spec_dir, usage, produced, generation
            except Exception as fallback_error:
                raise RuntimeError(
                    f"model provider failed ({provider_error}); local CLI fallback failed ({fallback_error})"
                ) from provider_error
        parsed = _extract_json(text)
        # Some model responses contain the SDD prose but omit the requested
        # implementation files. Retry once with a focused implementation-only
        # instruction so the run cannot silently finish without code.
        if not provider.simulation and (not parsed or not isinstance(parsed.get("files"), dict) or not parsed.get("files")):
            retry_prompt = f"""The previous response for task '{task.title}' omitted implementation files. Return ONLY valid JSON with a non-empty `files` object mapping repository-relative source and test paths to complete file contents. Implement the requirements and acceptance scenarios below; do not return prose or placeholders.\nRequirements: {'; '.join(r.description for r in task.requirements)}\nAcceptance: {'; '.join(s.then for s in task.acceptance_scenarios)}"""
            try:
                retry_text, retry_usage = provider.complete(retry_prompt)
                retry_parsed = _extract_json(retry_text)
                if retry_parsed and isinstance(retry_parsed.get("files"), dict) and retry_parsed.get("files"):
                    parsed = parsed or {}
                    parsed["files"] = retry_parsed["files"]
                    usage.input_tokens += retry_usage.input_tokens
                    usage.output_tokens += retry_usage.output_tokens
                    usage.latency_ms += retry_usage.latency_ms
            except Exception:
                # Continue to the local workspace-aware fallback below.
                pass
        if not provider.simulation and (not parsed or not isinstance(parsed.get("files"), dict) or not parsed.get("files")):
            # Chat providers cannot inspect the checked-out workspace. Hand off
            # to the local Codex workflow, which can read and modify the repo.
            return self._run_cli(task, workspace, "codex", "codex")
        if provider.simulation:
            parsed = {
                "proposal": f"# Proposal\n\n## Context\n{task.title}\n\n## Requirements\n{requirements}\n\n## Acceptance\n{scenarios}",
                "design": f"# Design\n\n## Requirements\n{requirements}\n\n## Design\nThis is a dry-run document; no model implementation was generated.",
                "tasks": f"# Tasks\n\n## Tasks\n- [ ] Configure a real model provider\n- [ ] Implement the requirements: {', '.join(r.id for r in task.requirements) or 'none'}\n\n## Validation\n{scenarios}",
                "files": {},
            }
        if not parsed:
            parsed = {"proposal": "", "design": text, "tasks": "", "files": {}}
        spec = workspace / "openspec"
        spec.mkdir(exist_ok=True)
        paths = {}
        for filename in ("proposal.md", "design.md", "tasks.md"):
            path = spec / filename
            path.write_text(str(parsed.get(filename[:-3], "")).strip() + "\n", encoding="utf-8")
            paths[filename] = str(path)
        files = parsed.get("files") if isinstance(parsed.get("files"), dict) else {}
        applied = []
        for relative, content in files.items():
            path = (workspace / str(relative)).resolve()
            if workspace.resolve() not in path.parents:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
            applied.append(str(path.relative_to(workspace)))
        code = workspace / "generated_code.md"
        code_body = "\n\n".join(f"// {name}\n{content}" for name, content in files.items())
        code.write_text("# Generated Code\n\n" + (code_body or "No implementation was generated.") + "\n", encoding="utf-8")
        paths["code"] = str(code)
        if files and task.build_command:
            build_command = task.build_command.replace("./mvnw", "mvnw.cmd", 1) if task.build_command.startswith("./mvnw") else task.build_command
            check = subprocess.run(build_command, cwd=workspace, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            if check.returncode != 0:
                # A chat response can contain plausible but incompatible code;
                # let the workspace-aware workflow repair it before evaluation.
                return self._run_cli(task, workspace, "codex", "codex")
        return spec, usage, paths, {"mode": "simulation" if provider.simulation else "model", "response_parsed": bool(_extract_json(text)), "files_requested": len(files), "files_applied": applied, "implementation_applied": bool(applied)}

    def _run_cli(self, task, workspace, model, client):
        """Drive OpenSpec through a local Codex or OpenCode CLI."""
        change = "eval-" + re.sub(r"[^a-z0-9]+", "-", task.id.lower()).strip("-")
        requirements = "\n".join(f"- {r.id}: {r.description}" for r in task.requirements) or "- No requirements supplied"
        before = {str(p.relative_to(workspace)): p.stat().st_mtime_ns for p in workspace.rglob("*") if p.is_file()}
        init = subprocess.run(["openspec.cmd", "init", str(workspace), "--tools", client], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        created = subprocess.run(["openspec.cmd", "new", "change", change, "--description", task.title], cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        skill_name = "Codex" if client == "codex" else "OpenCode"
        prompt = f"""Use the OpenSpec CLI and the installed OpenSpec {skill_name} integration in this workspace. Implement task '{task.title}' end to end. Requirements: {'; '.join(r.description for r in task.requirements)}. Acceptance: {'; '.join(s.then for s in task.acceptance_scenarios)}.
First create all OpenSpec artifacts for change {change}; then apply the change and write the actual implementation and regression tests into the repository. Do not only describe changes.
Inspect the existing source before editing. Preserve all existing public APIs, method signatures, entity fields, controller behavior, and unrelated files. Make the smallest focused change possible; do not rewrite whole classes or replace existing methods. Run the configured build and test commands after implementation, and fix any compilation or test failures you introduce before finishing."""
        requested = model.split(":", 1)[1] if ":" in model else None
        if client == "codex":
            command = ["codex.cmd", "exec", "--cd", str(workspace), "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
            if requested: command.extend(["--model", requested])
            command.append(prompt)
        else:
            command = ["opencode.cmd", "run", "--dir", str(workspace), "--format", "json", "--auto"]
            if requested: command.extend(["--model", requested])
            command.append(prompt)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        # Give the workspace-aware agent one chance to correct compile/test
        # regressions before the evaluator performs its authoritative checks.
        build_result = None
        if task.build_command:
            build_command = task.build_command.replace("./mvnw", "mvnw.cmd", 1) if task.build_command.startswith("./mvnw") else task.build_command
            build_result = subprocess.run(build_command, cwd=workspace, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if build_result is not None and build_result.returncode != 0:
            repair_prompt = f"The implementation for '{task.title}' was generated, but the build failed. Inspect the compiler output below, repair only the introduced changes while preserving existing APIs, then rerun the build and tests.\n{(build_result.stdout + build_result.stderr)[-6000:]}"
            if client == "codex":
                repair_command = ["codex.cmd", "exec", "--cd", str(workspace), "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", repair_prompt]
            else:
                repair_command = ["opencode.cmd", "run", "--dir", str(workspace), "--format", "json", "--auto", repair_prompt]
            repair = subprocess.run(repair_command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            result = repair
        spec = workspace / "openspec" / "changes" / change
        if not spec.exists(): spec = workspace / "openspec"
        documents = list(spec.rglob("*.md")) if spec.exists() else []
        if not documents: documents = list((workspace / "openspec").rglob("*.md")) if (workspace / "openspec").exists() else []
        # OpenSpec CLI versions may create only a change README. Ensure the
        # evaluator still archives the three required SDD artifacts.
        artifact_dir = spec if spec.exists() else (workspace / "openspec")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        defaults = {
            "proposal.md": f"# Proposal\n\n## Requirements\n{requirements}\n",
            "design.md": f"# Design\n\n## Design\nImplement the requested change while preserving existing APIs.\n\n## Requirements\n{requirements}\n",
            "tasks.md": f"# Tasks\n\n## Tasks\n" + "\n".join(f"- [ ] {r.description}" for r in task.requirements) + "\n",
        }
        for filename, content in defaults.items():
            target = artifact_dir / filename
            if not target.exists():
                target.write_text(content, encoding="utf-8")
                documents.append(target)
        paths = {p.name: str(p) for p in documents}
        code_files = [p for p in workspace.rglob("*") if p.is_file() and p.suffix in {".java", ".kt", ".py", ".ts", ".js", ".rs"}]
        ignored = {"target", ".opencode", "node_modules", "dist", "build"}
        applied = [str(p.relative_to(workspace)) for p in code_files if not (ignored & set(p.relative_to(workspace).parts)) and "openspec" not in p.parts and (str(p.relative_to(workspace)) not in before or p.stat().st_mtime_ns > before[str(p.relative_to(workspace))])]
        usage = TokenUsage(input_tokens=0, output_tokens=0, estimated=True, provider=f"{client}-cli", mode="model")
        code = workspace / "generated_code.md"
        code.write_text("# Generated Code\n\n" + ("\n\n".join(f"// {p}\n{(workspace / p).read_text(encoding='utf-8', errors='replace')}" for p in applied[:20]) or "No implementation files detected."), encoding="utf-8")
        paths["code"] = str(code)
        cli_output = (result.stdout + result.stderr)[-4000:]
        generation = {"mode": "model", "response_parsed": True, "files_requested": len(applied), "files_applied": applied, "implementation_applied": bool(applied), "openspec_exit": created.returncode, "client": client, "model": requested, "selection": f"{client}:{requested}" if requested else client, "cli_exit": result.returncode, "cli_output": cli_output, f"{client}_exit": result.returncode, f"{client}_output": cli_output}
        return (spec if spec.exists() else workspace), usage, paths, generation


class SuperpowersAdapter(ToolAdapter):
    """Run a Superpowers-style spec, plan, implement and verify workflow.

    An external command may still be supplied through SUPERPOWERS_COMMAND, but
    the built-in workflow keeps the adapter usable on a clean local machine.
    """
    def run(self, task, workspace, model):
        import os
        if model.startswith("codex") or model.startswith("opencode"):
            return self._run_cli_workflow(task, workspace, model, "codex" if model.startswith("codex") else "opencode")
        command = os.getenv("SUPERPOWERS_COMMAND")
        if command:
            # External integrations can opt in without changing the evaluator API.
            args = command.split()
            result = subprocess.run(args, cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            if result.returncode:
                raise RuntimeError(f"Superpowers command failed ({result.returncode}): {(result.stdout + result.stderr)[-2000:]}")

        requirements = "\n".join(f"- {r.id}: {r.description}" for r in task.requirements) or "- No requirements supplied"
        scenarios = "\n".join(f"- {s.id}: Given {s.given}; when {s.when}; then {s.then}" for s in task.acceptance_scenarios) or "- No acceptance scenarios supplied"
        prompt = f"""You are a Superpowers software-development agent. Follow the workflow: understand the request, write a concise specification, make a concrete implementation plan, implement the change, and add tests. Return ONLY valid JSON with keys spec, plan, tasks, files (object mapping repository-relative paths to complete file contents).
Task: {task.title}
Requirements:
{requirements}
Acceptance scenarios:
{scenarios}
Constraints:
{chr(10).join('- ' + c for c in task.constraints) or '- None'}
The files object must contain the actual implementation and tests. Do not return placeholders or prose outside the JSON."""
        provider = provider_for(model)
        try:
            text, usage = provider.complete(prompt)
        except Exception as provider_error:
            if provider.simulation:
                raise
            try:
                spec_dir, usage, produced, generation = self._run_cli_workflow(task, workspace, "codex", "codex")
                generation["provider_error"] = f"{type(provider_error).__name__}: {provider_error}"
                generation["fallback"] = "codex-cli"
                return spec_dir, usage, produced, generation
            except Exception as fallback_error:
                raise RuntimeError(
                    f"model provider failed ({provider_error}); local CLI fallback failed ({fallback_error})"
                ) from provider_error
        parsed = _extract_json(text)
        if provider.simulation:
            parsed = {"spec": f"# Specification\n\n## Requirements\n{requirements}\n\n## Acceptance\n{scenarios}", "plan": "# Plan\n\nDry-run only; no implementation was generated.", "tasks": "# Tasks\n\n- [ ] Configure a real model provider", "files": {}}
        if not parsed:
            parsed = {"spec": "", "plan": text, "tasks": "", "files": {}}
        spec = workspace / "superpowers"
        spec.mkdir(exist_ok=True)
        paths = {}
        for key, filename in (("spec", "spec.md"), ("plan", "plan.md"), ("tasks", "tasks.md")):
            path = spec / filename
            path.write_text(str(parsed.get(key, "")).strip() + "\n", encoding="utf-8")
            paths[filename] = str(path)
        files = parsed.get("files") if isinstance(parsed.get("files"), dict) else {}
        applied = []
        for relative, content in files.items():
            path = (workspace / str(relative)).resolve()
            if workspace.resolve() not in path.parents:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content), encoding="utf-8")
            applied.append(str(path.relative_to(workspace)))
        code = workspace / "generated_code.md"
        code.write_text("# Generated Code\n\n" + ("\n\n".join(f"// {name}\n{content}" for name, content in files.items()) or "No implementation was generated.") + "\n", encoding="utf-8")
        paths["code"] = str(code)
        return spec, usage, paths, {"mode": "simulation" if provider.simulation else "model", "response_parsed": bool(_extract_json(text)), "files_requested": len(files), "files_applied": applied, "implementation_applied": bool(applied), "workflow": "superpowers", "external_command": bool(command)}

    def _run_cli_workflow(self, task, workspace, model, client):
        """Run the Superpowers workflow using a workspace-aware local CLI."""
        import os
        before = {str(p.relative_to(workspace)): p.stat().st_mtime_ns for p in workspace.rglob("*") if p.is_file()}
        requirements = "; ".join(r.description for r in task.requirements) or "No requirements supplied"
        acceptance = "; ".join(s.then for s in task.acceptance_scenarios) or "No acceptance scenarios supplied"
        prompt = f"""Follow a Superpowers spec-plan-implement-test workflow for '{task.title}'. First write a concise specification, implementation plan, and task checklist under superpowers/. Then implement the change and regression tests in the repository. Requirements: {requirements}. Acceptance: {acceptance}. Inspect existing code, preserve public APIs, and run the configured build and tests."""
        requested = model.split(":", 1)[1] if ":" in model else None
        if client == "codex":
            command = ["codex.cmd", "exec", "--cd", str(workspace), "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
            if requested: command.extend(["--model", requested])
        else:
            command = ["opencode.cmd", "run", "--dir", str(workspace), "--format", "json", "--auto"]
            if requested: command.extend(["--model", requested])
        command.append(prompt)
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        spec = workspace / "superpowers"; spec.mkdir(exist_ok=True)
        defaults = {"spec.md": f"# Specification\n\n## Requirements\n{requirements}\n", "plan.md": "# Plan\n\nImplement the requested change and verify it.\n", "tasks.md": f"# Tasks\n\n- [ ] {requirements}\n"}
        for name, content in defaults.items():
            target = spec / name
            if not target.exists(): target.write_text(content, encoding="utf-8")
        ignored = {"target", ".opencode", "node_modules", "dist", "build"}
        applied = [str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file() and p.suffix in {".java", ".kt", ".py", ".ts", ".js", ".rs"} and not (ignored & set(p.relative_to(workspace).parts)) and "superpowers" not in p.parts and (str(p.relative_to(workspace)) not in before or p.stat().st_mtime_ns > before[str(p.relative_to(workspace))])]
        code = workspace / "generated_code.md"; code.write_text("# Generated Code\n\n" + ("\n".join(applied) or "No implementation files detected."), encoding="utf-8")
        paths = {name: str(spec / name) for name in defaults}; paths["code"] = str(code)
        usage = TokenUsage(input_tokens=0, output_tokens=0, estimated=True, provider=f"{client}-cli", mode="model")
        cli_output = (result.stdout + result.stderr)[-4000:]
        generation = {"mode": "model", "response_parsed": True, "files_requested": len(applied), "files_applied": applied, "implementation_applied": bool(applied), "workflow": "superpowers", "client": client, "model": requested, "selection": f"{client}:{requested}" if requested else client, "cli_exit": result.returncode, "cli_output": cli_output, f"{client}_exit": result.returncode, f"{client}_output": cli_output}
        return spec, usage, paths, generation


ADAPTERS = {"openspec": OpenSpecAdapter(), "superpowers": SuperpowersAdapter()}
