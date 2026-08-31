from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import urllib.request
import zipfile
import difflib

from .adapters import ADAPTERS
from .models import RunResult, TaskSpec, now


def evaluate(task: TaskSpec, tool: str, model: str, workspace: str | None = None) -> RunResult:
    run_id = uuid.uuid4().hex[:12]
    run_root = Path(workspace or ".sdd-runs") / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    steps, artifacts, execution_logs = [], {}, []
    root = run_root
    total_started = time.perf_counter()
    started_at = now()

    def add_step(name: str, status: str, started: float, detail: str = ""):
        steps.append({"name": name, "status": status, "duration_ms": int((time.perf_counter() - started) * 1000), "detail": detail})

    def add_log(name: str, output: str):
        execution_logs.append(f"===== {name} =====\n{output or '(no output)'}")

    def reference_comparison(root: Path, generated: str) -> tuple[float, str]:
        """Compare generated implementation with the task's reference commit."""
        if not task.reference_commit or not (root / ".git").exists():
            return 0.0, "No reference commit available for comparison."
        try:
            ref = subprocess.run(
                ["git", "show", "--format=", "--no-ext-diff", task.reference_commit],
                cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            if ref.returncode:
                return 0.0, f"Reference commit unavailable: {ref.stderr.strip()[-500:]}"
            added = "\n".join(
                line[1:] for line in ref.stdout.splitlines()
                if line.startswith("+") and not line.startswith("+++")
            )
            token = lambda value: set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]", value))
            reference_tokens, generated_tokens = token(added), token(generated)
            lexical = len(reference_tokens & generated_tokens) / max(1, len(reference_tokens | generated_tokens))
            ref_lines = [line.strip() for line in added.splitlines() if line.strip()]
            gen_lines = [line.strip() for line in generated.splitlines() if line.strip()]
            formatting = difflib.SequenceMatcher(None, ref_lines, gen_lines).ratio()
            score = round((lexical * 0.7 + formatting * 0.3) * 100, 2)
            return score, f"Logic token similarity {lexical:.1%}; code-format/sequence similarity {formatting:.1%}."
        except (OSError, subprocess.SubprocessError) as error:
            return 0.0, f"Reference comparison failed: {error}"

    def command_for_workspace(command: str, cwd: Path) -> str:
        if command.startswith("./mvnw"):
            suffix = command[len("./mvnw"):]
            if os.name == "nt":
                if (cwd / "mvnw.cmd").exists():
                    return "mvnw.cmd" + suffix
                # Some repositories publish only the POSIX wrapper. Fall
                # back to Maven when a native wrapper is unavailable.
                return "mvn" + suffix
            return command
        return command

    def prepare_node_dependencies(cwd: Path):
        """Prepare a JS workspace and make Corepack-managed tools visible to scripts."""
        package_file = cwd / "package.json"
        if not package_file.exists():
            return None, None
        try:
            package = json.loads(package_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"could not read package.json: {error}") from error
        package_manager = str(package.get("packageManager", "")).split("@", 1)[0].lower()
        if package_manager not in {"npm", "yarn", "pnpm"}:
            if (cwd / "yarn.lock").exists():
                package_manager = "yarn"
            elif (cwd / "pnpm-lock.yaml").exists():
                package_manager = "pnpm"
            elif (cwd / "package-lock.json").exists():
                package_manager = "npm"
        if package_manager not in {"npm", "yarn", "pnpm"}:
            return None, None

        env = os.environ.copy()
        corepack = shutil.which("corepack") or shutil.which("corepack.cmd")
        if package_manager == "yarn":
            # Keep Yarn's link registry inside the disposable checkout. Some
            # repositories run `yarn setup` during pretest and a stale global
            # link can make an otherwise valid checkout fail before Jest runs.
            link_folder = cwd / ".yarn-links"
            link_folder.mkdir(parents=True, exist_ok=True)
            env["YARN_LINK_FOLDER"] = str(link_folder)
        # npm lifecycle scripts inherit PATH. A local shim lets commands such as
        # webpack's `pretest: yarn lint` work even when Corepack shims cannot be
        # installed globally on a locked-down Windows machine.
        if package_manager == "yarn" and not shutil.which("yarn") and corepack:
            shim = cwd / "yarn.cmd"
            if not shim.exists():
                shim.write_text("@echo off\r\ncorepack yarn --link-folder \"%~dp0.yarn-links\" %*\r\n", encoding="utf-8")
            env["PATH"] = str(cwd) + os.pathsep + env.get("PATH", "")
        if (cwd / "node_modules").exists():
            return env, "node_modules already present"

        if package_manager == "yarn":
            if not corepack and not shutil.which("yarn"):
                raise RuntimeError("Yarn is required by package.json but neither yarn nor Corepack is available")
            command = ["corepack.cmd" if os.name == "nt" else "corepack", "yarn", "install", "--frozen-lockfile"] if corepack else ["yarn", "install", "--frozen-lockfile"]
        elif package_manager == "pnpm":
            if not corepack and not shutil.which("pnpm"):
                raise RuntimeError("pnpm is required by package.json but neither pnpm nor Corepack is available")
            command = ["corepack.cmd" if os.name == "nt" else "corepack", "pnpm", "install", "--frozen-lockfile"] if corepack else ["pnpm", "install", "--frozen-lockfile"]
        else:
            npm = "npm.cmd" if os.name == "nt" else "npm"
            command = [npm, "ci"] if (cwd / "package-lock.json").exists() else [npm, "install"]
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, env=env)
        detail = (result.stdout + result.stderr)[-2000:]
        add_log("Install project dependencies", result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(f"dependency installation failed ({result.returncode}): {detail}")
        return env, f"{ ' '.join(command) }\n{detail}".strip()

    def restore_baseline(root: Path):
        """Discard any stale tracked/untracked changes before the SDD tool runs."""
        if not (root / ".git").exists():
            return "not a git checkout"
        # Keep disposable checkouts in repository-native line endings. On
        # Windows a user's global `core.autocrlf=true` otherwise rewrites LF
        # files to CRLF and makes format/lint checks fail before tests run.
        config = subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if config.returncode:
            raise RuntimeError(f"could not configure checkout line endings: {(config.stdout + config.stderr)[-1000:]}")
        reset = subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=root, capture_output=True, text=True, timeout=120)
        clean = subprocess.run(["git", "clean", "-fdx"], cwd=root, capture_output=True, text=True, timeout=120)
        if reset.returncode or clean.returncode:
            raise RuntimeError(f"baseline restore failed: {(reset.stdout + reset.stderr + clean.stdout + clean.stderr)[-2000:]}")
        return "git reset --hard HEAD && git clean -fdx"

    def normalize_text_line_endings(root: Path):
        """Normalize tracked text files for formatters that require LF."""
        if not (root / ".git").exists():
            return
        listing = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True, timeout=120
        )
        if listing.returncode:
            return
        for raw_path in listing.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = root / os.fsdecode(raw_path)
            try:
                data = path.read_bytes()
                if b"\0" not in data and b"\r\n" in data:
                    path.write_bytes(data.replace(b"\r\n", b"\n"))
            except OSError:
                continue

    def cleanup_run_root():
        """Remove the disposable checkout; generated artifacts are already stored in the result."""
        if run_root.exists():
            shutil.rmtree(run_root, ignore_errors=True)

    try:
        started = time.perf_counter()
        root = run_root
        if task.archive_url:
            archive = run_root / "repository.zip"
            urllib.request.urlretrieve(task.archive_url, archive)
            extract_dir = run_root / "repo-extracted"
            extract_dir.mkdir()
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(extract_dir)
            children = [p for p in extract_dir.iterdir() if p.is_dir()]
            root = children[0] if len(children) == 1 else extract_dir
            add_step("Download project ZIP", "passed", started, task.archive_url)
        elif task.repository:
            root = run_root / "repo"
            repository_url = task.repository.rstrip("/")
            candidates = [repository_url]
            match = re.search(r"github\.com/([^/]+)/([^/]+?)(?:\.git)?$", repository_url)
            if match:
                owner, name = match.groups()
                candidates.extend([f"https://gitcode.com/gh_mirrors/{owner[:1]}/{name}.git", f"https://gitee.com/mirrors/{name}.git"])
            clone = None
            clone_source = repository_url
            for candidate in candidates:
                attempt = subprocess.run(["git", "clone", "--depth", "1", candidate, str(root)], capture_output=True, text=True, timeout=300)
                clone = attempt
                if attempt.returncode == 0:
                    clone_source = candidate
                    break
                if root.exists():
                    shutil.rmtree(root, ignore_errors=True)
            if clone.returncode:
                # GitHub's archive endpoint provides a useful offline-friendly fallback.
                archive = run_root / "repository.zip"
                try:
                    revision = task.revision or "main"
                    if "gitee.com/" in repository_url:
                        archive_urls = [f"{repository_url}/repository/archive/{revision}.zip", f"{repository_url}/repository/archive/master.zip", f"{repository_url}/archive/{revision}.zip"]
                    else:
                        archive_urls = [f"{repository_url}/archive/{revision}.zip", f"{repository_url}/archive/master.zip"]
                        if match:
                            owner, name = match.groups()
                            archive_urls.extend([f"https://gitcode.com/gh_mirrors/{owner[:1]}/{name}/archive/{revision}.zip", f"https://gitee.com/mirrors/{name}/repository/archive/{revision}.zip"])
                    archive_url = archive_urls[0]
                    download_error = None
                    for candidate in archive_urls:
                        try:
                            urllib.request.urlretrieve(candidate, archive)
                            archive_url = candidate
                            break
                        except Exception as error:
                            download_error = error
                    else:
                        raise download_error or RuntimeError("archive download failed")
                    extract_dir = run_root / "repo-extracted"
                    extract_dir.mkdir()
                    with zipfile.ZipFile(archive) as zf:
                        zf.extractall(extract_dir)
                    children = [p for p in extract_dir.iterdir() if p.is_dir()]
                    root = children[0] if len(children) == 1 else extract_dir
                    add_step("Download repository ZIP", "passed", started, archive_url)
                except Exception as archive_error:
                    root = run_root
                    add_step("Repository acquisition", "failed", started, f"git: {(clone.stdout + clone.stderr)[-500:]} zip: {archive_error}")
            else:
                add_step("Clone repository", "passed", started, f"{root} ({clone_source})")
        add_step("Prepare workspace", "passed", started, str(root))

        started = time.perf_counter()
        baseline_detail = restore_baseline(root)
        add_step("Restore baseline", "passed", started, baseline_detail)

        started = time.perf_counter()
        execution_env, dependency_detail = prepare_node_dependencies(root)
        if dependency_detail is not None:
            add_step("Install project dependencies", "passed", started, dependency_detail)

        started = time.perf_counter()
        spec_dir, usage, produced, generation = ADAPTERS[tool].run(task, root, model)
        add_log("SDD generation", generation.get("cli_output", ""))
        documents = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in spec_dir.glob("*.md")}
        artifacts["documents"] = documents
        artifacts["paths"] = produced
        add_step("Generate SDD documents", "passed", started, ", ".join(documents))

        code_path = Path(produced["code"])
        artifacts["generated_code"] = code_path.read_text(encoding="utf-8", errors="replace")
        normalize_text_line_endings(root)
        source_files = [p for p in root.rglob("*") if p.is_file() and p.suffix in {".java", ".kt", ".py", ".ts", ".js", ".rs"}]
        artifacts["repository_code"] = "\n\n".join(f"// {p.relative_to(root)}\n{p.read_text(encoding='utf-8', errors='replace')}" for p in source_files[:20])
        artifacts["execution_log"] = "\n\n".join(execution_logs)
        if task.reference_commit or task.reference_pr_url or task.source_issue_url or task.reference_url:
            artifacts["reference"] = {
                "url": task.reference_url or task.source_issue_url or task.reference_pr_url,
                "provider": task.reference_provider,
                "repo": task.reference_repo,
                "code_lines": task.reference_code_lines,
                "code_lines_estimated": task.reference_code_estimated,
                "issue_url": task.source_issue_url,
                "issue_number": task.source_issue_number,
                "pr_url": task.reference_pr_url,
                "commit": task.reference_commit,
                "commit_url": task.reference_commit_url,
                "notes": task.reference_notes,
            }

        doc_text = "\n".join(documents.values())
        section_score = sum(bool(re.search(r"^##\s+" + name, doc_text, re.I | re.M)) for name in ("requirements", "design", "tasks")) / 3 * 100
        nonempty_score = min(100, sum(len(v.strip()) for v in documents.values()) / 12)
        doc_score = round(section_score * .6 + nonempty_score * .4, 2)
        reference_score, reference_basis = reference_comparison(root, artifacts.get("generated_code", ""))
        build_ok, test_ok = False, False

        if task.build_command:
            started = time.perf_counter()
            command = command_for_workspace(task.build_command, root)
            result = subprocess.run(command, cwd=root, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=execution_env)
            build_ok = result.returncode == 0
            add_log("Build project", (result.stdout or "") + (result.stderr or ""))
            add_step("Build project", "passed" if build_ok else "failed", started, ((result.stdout or "") + (result.stderr or ""))[-2000:])

        if task.test_command:
            started = time.perf_counter()
            result = subprocess.run(command_for_workspace(task.test_command, root), cwd=root, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300, env=execution_env)
            test_ok = result.returncode == 0
            add_log("Run test command", (result.stdout or "") + (result.stderr or ""))
            add_step("Run test command", "passed" if test_ok else "failed", started, ((result.stdout or "") + (result.stderr or ""))[-2000:])
        else:
            add_step("Run test command", "failed", time.perf_counter(), "No test command configured; test score is 0.")

        code_score, test_score = (100 if build_ok else 0), (100 if test_ok else 0)
        total_tokens = usage.input_tokens + usage.output_tokens
        efficiency = max(0, 100 - min(100, total_tokens / 100))
        if usage.provider.endswith("-cli") and total_tokens == 0:
            efficiency = 0
        # A dry-run, empty model response, or no applied implementation can never pass as a real evaluation.
        real_execution = generation.get("mode") == "model" and generation.get("response_parsed") and generation.get("implementation_applied")
        if not real_execution:
            doc_score = 0
            code_score = 0
            test_score = 0
            reference_score = 0
            if generation.get("mode") == "simulation":
                efficiency = 0
        score = round(doc_score * .2 + code_score * .3 + test_score * .3 + reference_score * .1 + efficiency * .1, 2)
        scoring = [
            {"dimension": "Document quality", "weight": 20, "score": doc_score, "basis": "Section presence (60%) plus non-empty document content (40%); simulation has no quality credit."},
            {"dimension": "Code quality", "weight": 30, "score": code_score, "basis": "A real model response must apply implementation files, then the build command must pass."},
            {"dimension": "Test quality", "weight": 30, "score": test_score, "basis": "A separately configured test command must pass; missing test command scores 0."},
            {"dimension": "Efficiency", "weight": 10, "score": efficiency, "basis": f"{total_tokens} tokens reported by {usage.provider}/{usage.mode}; CLI token usage unavailable is scored 0, and simulation receives no quality credit."},
            {"dimension": "Reference comparison", "weight": 10, "score": reference_score, "basis": reference_basis},
        ]
        add_step("Calculate score", "passed", total_started, f"Total score: {score}")
        metrics = {"document": doc_score, "code": code_score, "tests": test_score, "reference": reference_score, "efficiency": efficiency, "total_duration_ms": int((time.perf_counter() - total_started) * 1000), "token_input": usage.input_tokens, "token_output": usage.output_tokens, "generation": generation}
        status = "passed" if real_execution and test_ok else "incomplete" if not real_execution else "failed"
        finished_at = now()
        duration_ms = int((time.perf_counter() - total_started) * 1000)
        metrics["total_duration_ms"] = duration_ms
        cleanup_started = time.perf_counter()
        cleanup_run_root()
        add_step("Clean workspace", "passed", cleanup_started, "Disposable run workspace removed")
        return RunResult(run_id=run_id, task_id=task.id, status=status, score=score, metrics=metrics, steps=steps, artifacts=artifacts, scoring_basis=scoring, token_usage=usage, execution_mode=generation.get("mode", "unknown"), generation_status="applied" if generation.get("implementation_applied") else "not_applied", validation={"build_passed": build_ok, "tests_passed": test_ok, "real_execution": real_execution}, started_at=started_at, finished_at=finished_at, duration_ms=duration_ms)
    except Exception as error:
        # A provider timeout or tool failure must still produce a queryable,
        # scored Run record. Capture a failure artifact before deleting the
        # disposable checkout so the dashboard never shows a blank result.
        message = str(error)
        if not artifacts.get("documents"):
            artifacts["documents"] = {"generation-error.md": f"# Generation failed\n\n{message}\n"}
        if "generated_code" not in artifacts:
            artifacts["generated_code"] = f"# Generated Code\n\nGeneration failed before implementation was produced.\n\nError: {message}\n"
        if not artifacts.get("repository_code"):
            try:
                source_files = [p for p in root.rglob("*") if p.is_file() and p.suffix in {".java", ".kt", ".py", ".ts", ".js", ".rs"}]
                artifacts["repository_code"] = "\n\n".join(f"// {p.relative_to(root)}\n{p.read_text(encoding='utf-8', errors='replace')}" for p in source_files[:20])
            except Exception:
                artifacts["repository_code"] = ""
        artifacts["execution_log"] = "\n\n".join(execution_logs)
        add_log("Run error", message)
        artifacts["execution_log"] = "\n\n".join(execution_logs)
        add_step("Run error", "failed", total_started, str(error))
        cleanup_started = time.perf_counter()
        cleanup_run_root()
        add_step("Clean workspace", "passed", cleanup_started, "Disposable run workspace removed after failure")
        finished_at = now()
        duration_ms = int((time.perf_counter() - total_started) * 1000)
        scoring = [
            {"dimension": "Document quality", "weight": 30, "score": 0, "basis": "Generation failed; no quality credit."},
            {"dimension": "Code quality", "weight": 30, "score": 0, "basis": "Generation failed before implementation could be applied."},
            {"dimension": "Test quality", "weight": 30, "score": 0, "basis": "Generation failed; tests were not evaluated."},
            {"dimension": "Efficiency", "weight": 10, "score": 0, "basis": "Run failed before reliable token accounting was available."},
        ]
        return RunResult(run_id=run_id, task_id=task.id, status="failed", score=0.0, metrics={"document": 0, "code": 0, "tests": 0, "efficiency": 0, "total_duration_ms": duration_ms}, scoring_basis=scoring, error=message, steps=steps, artifacts=artifacts, started_at=started_at, finished_at=finished_at, duration_ms=duration_ms)
