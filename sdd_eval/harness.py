"""Executable-oracle evaluation backends for Benchmark V2.

The local backend is intentionally limited to trusted repositories and command
specifications. It establishes grading semantics before the later Docker
backend adds a security boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fnmatch
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Sequence

from .models import (
    BenchmarkInstance,
    EvaluationOracle,
    EvaluationResult,
    InstanceValidationResult,
    Prediction,
)


HARNESS_VERSION = "local-v1"

SCORE_WEIGHTS = {"functional": 0.50, "code_quality": 0.25, "documentation": 0.25}


@dataclass
class CommandResult:
    passed: bool
    returncode: int
    output: str


@dataclass
class CheckoutResult:
    patch_applied: bool = False
    build_passed: bool = False
    fail_to_pass_passed: int = 0
    pass_to_pass_passed: int = 0
    forbidden_changes: list[str] = field(default_factory=list)
    logs: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    error_kind: str | None = None


class LocalEvaluationBackend:
    """Run an executable-oracle evaluation directly on the host.

    This backend executes repository-provided commands and must only be used
    with trusted inputs. Production or multi-tenant use requires DockerBackend.
    """

    name = HARNESS_VERSION

    def environment_digest(self, instance: BenchmarkInstance) -> str:
        payload = {
            "backend": self.name,
            "environment_id": instance.environment_id,
            "language": instance.language,
            "environment": instance.environment.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _run(self, command: Sequence[str], cwd: Path, timeout: int) -> CommandResult:
        try:
            process = subprocess.run(
                list(command), cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
            output = (process.stdout or "") + (process.stderr or "")
            return CommandResult(process.returncode == 0, process.returncode, output)
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else error.stdout or ""
            stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else error.stderr or ""
            return CommandResult(False, 124, f"{stdout}{stderr}\nCommand timed out after {timeout}s")
        except OSError as error:
            return CommandResult(False, 127, str(error))

    def _prepare_checkout(self, instance: BenchmarkInstance, destination: Path) -> tuple[Path | None, str]:
        clone = self._run(["git", "clone", "--no-hardlinks", instance.repo, str(destination)], destination.parent, 600)
        if not clone.passed:
            return None, clone.output
        checkout = self._run(["git", "checkout", "--detach", instance.base_commit], destination, 180)
        if not checkout.passed:
            return None, checkout.output
        return destination, clone.output + checkout.output

    def _apply_patch(self, root: Path, patch: str, label: str) -> CommandResult:
        if not patch.strip():
            return CommandResult(False, 1, f"{label} is empty")
        try:
            process = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", "-"], cwd=root,
                input=patch.encode("utf-8"), capture_output=True, timeout=120,
            )
            output = (process.stdout or b"").decode("utf-8", "replace")
            output += (process.stderr or b"").decode("utf-8", "replace")
            return CommandResult(process.returncode == 0, process.returncode, output)
        except (OSError, subprocess.TimeoutExpired) as error:
            return CommandResult(False, 1, str(error))

    def _changed_paths(self, root: Path) -> list[str]:
        tracked = self._run(["git", "diff", "--name-only", "--diff-filter=ACMRD"], root, 60)
        untracked = self._run(["git", "ls-files", "--others", "--exclude-standard"], root, 60)
        paths = []
        for command in (tracked, untracked):
            if command.passed:
                paths.extend(line.strip().replace("\\", "/") for line in command.output.splitlines() if line.strip())
        return sorted(set(paths))

    def _forbidden_changes(self, paths: list[str], patterns: list[str]) -> list[str]:
        return sorted({path for path in paths for pattern in patterns if fnmatch.fnmatch(path, pattern)})

    def _working_directory(self, root: Path, instance: BenchmarkInstance) -> Path:
        target = (root / instance.environment.working_directory).resolve()
        if target != root.resolve() and root.resolve() not in target.parents:
            raise ValueError("working_directory escapes the repository")
        if not target.is_dir():
            raise ValueError(f"working_directory does not exist: {instance.environment.working_directory}")
        return target

    def _expand_test_command(self, command: list[str], selectors: list[str]) -> list[str]:
        expanded: list[str] = []
        found_placeholder = False
        for argument in command:
            if argument == "{tests}":
                expanded.extend(selectors)
                found_placeholder = True
            elif "{tests}" in argument:
                expanded.append(argument.replace("{tests}", ",".join(selectors)))
                found_placeholder = True
            else:
                expanded.append(argument)
        return expanded if found_placeholder else expanded + selectors

    def _run_setup(self, root: Path, instance: BenchmarkInstance, result: CheckoutResult) -> bool:
        cwd = self._working_directory(root, instance)
        for index, command in enumerate(instance.environment.setup_commands, start=1):
            command_result = self._run(command, cwd, instance.environment.setup_timeout_seconds)
            result.logs[f"setup_{index}"] = command_result.output
            if not command_result.passed:
                result.error = f"setup command {index} failed"
                result.error_kind = "environment_error"
                return False
        return True

    def _run_build_and_tests(self, root: Path, instance: BenchmarkInstance, oracle: EvaluationOracle, result: CheckoutResult) -> None:
        cwd = self._working_directory(root, instance)
        if instance.environment.build_command:
            build = self._run(instance.environment.build_command, cwd, instance.environment.build_timeout_seconds)
            result.logs["build"] = build.output
            result.build_passed = build.passed
            if not build.passed:
                result.error = "build command failed"
                result.error_kind = "build_failed"
                return
        else:
            result.build_passed = True
        for group_name, selectors in (("fail_to_pass", oracle.fail_to_pass), ("pass_to_pass", oracle.pass_to_pass)):
            passed = 0
            outputs = []
            for selector in selectors:
                command = self._expand_test_command(instance.environment.test_command, [selector])
                test = self._run(command, cwd, instance.environment.test_timeout_seconds)
                outputs.append(f"===== {selector} (exit {test.returncode}) =====\n{test.output}")
                passed += int(test.passed)
            result.logs[group_name] = "\n".join(outputs)
            if group_name == "fail_to_pass":
                result.fail_to_pass_passed = passed
            else:
                result.pass_to_pass_passed = passed

    @staticmethod
    def _quality_scores(prediction: Prediction, execution: CheckoutResult, functional_score: float) -> tuple[float, float]:
        """Return code and documentation scores from observable prediction evidence.

        Older fixture predictions have no SDD artifacts; for those records we
        retain a conservative functional score for both dimensions so legacy
        results remain numerically stable. Generated predictions with explicit
        artifacts are scored on patch hygiene and document completeness.
        """
        if not prediction.artifacts.documents and not prediction.artifacts.trace_links:
            return functional_score, functional_score
        if execution.error_kind or not execution.patch_applied or execution.forbidden_changes or not execution.build_passed:
            code_score = 0.0
        else:
            patch_lines = prediction.model_patch.splitlines()
            additions = [line[1:] for line in patch_lines if line.startswith("+") and not line.startswith("+++")]
            hygiene_penalty = sum(line.rstrip() != line for line in additions)
            code_score = max(0.0, round(100.0 - min(40.0, hygiene_penalty * 5.0), 2))
        documents = {str(name): str(value).strip() for name, value in prediction.artifacts.documents.items()}
        non_empty = sum(bool(value) for value in documents.values())
        named_docs = sum(any(token in name.lower() for token in ("spec", "design", "requirement", "plan")) for name in documents)
        trace_links = prediction.artifacts.trace_links
        covered_links = sum(link.status == "covered" for link in trace_links)
        if not documents:
            documentation_score = 0.0
        else:
            documentation_score = min(
                100.0,
                round((min(non_empty, 2) / 2 * 60.0) + (min(named_docs, 2) / 2 * 20.0) + (covered_links > 0) * 20.0, 2),
            )
        return code_score, documentation_score

    def _execute_patch(self, instance: BenchmarkInstance, oracle: EvaluationOracle, model_patch: str, run_root: Path) -> CheckoutResult:
        result = CheckoutResult()
        root, acquisition_log = self._prepare_checkout(instance, run_root / "repo")
        result.logs["checkout"] = acquisition_log
        if root is None:
            result.error = "repository checkout failed"
            result.error_kind = "environment_error"
            return result
        try:
            if not self._run_setup(root, instance, result):
                return result
        except ValueError as error:
            result.error = str(error); result.error_kind = "environment_error"; return result
        patch = self._apply_patch(root, model_patch, "model patch")
        result.logs["model_patch"] = patch.output
        if not patch.passed:
            result.error = "model patch could not be applied"
            result.error_kind = "invalid_patch"
            return result
        result.patch_applied = True
        result.forbidden_changes = self._forbidden_changes(self._changed_paths(root), oracle.forbidden_paths)
        if result.forbidden_changes:
            result.error = "model patch modifies forbidden paths"
            result.error_kind = "invalid_patch"
            return result
        test_patch = self._apply_patch(root, oracle.test_patch, "test patch")
        result.logs["test_patch"] = test_patch.output
        if not test_patch.passed:
            result.error = "test patch could not be applied"
            result.error_kind = "harness_error"
            return result
        self._run_build_and_tests(root, instance, oracle, result)
        return result

    def evaluate(
        self,
        instance: BenchmarkInstance,
        oracle: EvaluationOracle,
        prediction: Prediction,
        workspace: str | Path | None = None,
    ) -> EvaluationResult:
        if instance.instance_id != oracle.instance_id or instance.instance_id != prediction.instance_id:
            raise ValueError("instance, oracle, and prediction identifiers must match")
        digest = self.environment_digest(instance)
        if not oracle.fail_to_pass:
            return EvaluationResult(
                prediction_id=prediction.prediction_id, instance_id=instance.instance_id,
                outcome="harness_error", prediction_hash=prediction.patch_hash,
                environment_digest=digest, harness_version=self.name,
                functional_metrics={"error": "FAIL_TO_PASS must not be empty"},
            )
        parent = Path(workspace) if workspace else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sdd-eval-v2-", dir=parent) as temporary:
            execution = self._execute_patch(instance, oracle, prediction.model_patch, Path(temporary))
        all_f2p = execution.fail_to_pass_passed == len(oracle.fail_to_pass)
        all_p2p = execution.pass_to_pass_passed == len(oracle.pass_to_pass)
        if execution.error_kind:
            outcome = execution.error_kind
        elif not all_f2p:
            outcome = "target_tests_failed"
        elif not all_p2p:
            outcome = "regression"
        else:
            outcome = "resolved"
        fail_to_pass_rate = execution.fail_to_pass_passed / len(oracle.fail_to_pass)
        pass_to_pass_rate = execution.pass_to_pass_passed / len(oracle.pass_to_pass) if oracle.pass_to_pass else 1.0
        # Functional quality is the 50% portion of the composite score. The
        # two executable test families are equally important within it.
        functional_score = 0.0 if execution.error_kind else round(((fail_to_pass_rate + pass_to_pass_rate) / 2) * 100, 2)
        code_quality_score, documentation_score = self._quality_scores(prediction, execution, functional_score)
        score = round(
            functional_score * SCORE_WEIGHTS["functional"]
            + code_quality_score * SCORE_WEIGHTS["code_quality"]
            + documentation_score * SCORE_WEIGHTS["documentation"],
            2,
        )
        documents = prediction.artifacts.documents
        trace_links = prediction.artifacts.trace_links
        covered_links = sum(link.status == "covered" for link in trace_links)
        return EvaluationResult(
            prediction_id=prediction.prediction_id,
            instance_id=instance.instance_id,
            outcome=outcome,
            resolved=outcome == "resolved",
            score=score,
            functional_score=functional_score,
            code_quality_score=code_quality_score,
            documentation_score=documentation_score,
            score_weights=SCORE_WEIGHTS.copy(),
            patch_applied=execution.patch_applied,
            build_passed=execution.build_passed,
            fail_to_pass_total=len(oracle.fail_to_pass),
            fail_to_pass_passed=execution.fail_to_pass_passed,
            pass_to_pass_total=len(oracle.pass_to_pass),
            pass_to_pass_passed=execution.pass_to_pass_passed,
            prediction_hash=prediction.patch_hash,
            environment_digest=digest,
            harness_version=self.name,
            functional_metrics={
                "error": execution.error,
                "forbidden_changes": execution.forbidden_changes,
                "score": functional_score,
                "functional_score": functional_score,
                "code_quality_score": code_quality_score,
                "documentation_score": documentation_score,
                "composite_score": score,
                "score_weights": SCORE_WEIGHTS.copy(),
                "fail_to_pass_rate": round(fail_to_pass_rate, 4),
                "pass_to_pass_rate": round(pass_to_pass_rate, 4),
                "logs": execution.logs,
            },
            sdd_metrics={
                "workflow": prediction.workflow,
                "document_count": len(documents),
                "documents": sorted(documents),
                "trace_link_count": len(trace_links),
                "covered_trace_links": covered_links,
            },
            efficiency_metrics={
                "input_tokens": prediction.token_usage.input_tokens,
                "output_tokens": prediction.token_usage.output_tokens,
                "estimated": prediction.token_usage.estimated,
                "generation_latency_ms": prediction.token_usage.latency_ms,
            },
        )

    def validate_instance(
        self,
        instance: BenchmarkInstance,
        oracle: EvaluationOracle,
        workspace: str | Path | None = None,
    ) -> InstanceValidationResult:
        if instance.instance_id != oracle.instance_id:
            raise ValueError("instance and oracle identifiers must match")
        digest = self.environment_digest(instance)
        errors: list[str] = []
        logs: dict[str, str] = {}
        if not oracle.fail_to_pass:
            errors.append("FAIL_TO_PASS must not be empty")
        if not oracle.gold_patch.strip():
            errors.append("gold patch must not be empty")
        if not oracle.test_patch.strip():
            errors.append("test patch must not be empty")
        if errors:
            return InstanceValidationResult(instance_id=instance.instance_id, valid=False, errors=errors, environment_digest=digest, harness_version=self.name)
        parent = Path(workspace) if workspace else None
        if parent:
            parent.mkdir(parents=True, exist_ok=True)
        baseline = self._validate_baseline(instance, oracle, parent)
        with tempfile.TemporaryDirectory(prefix="sdd-eval-gold-", dir=parent) as temporary:
            gold = self._execute_patch(instance, oracle, oracle.gold_patch, Path(temporary))
        logs.update({f"baseline_{key}": value for key, value in baseline.logs.items()})
        logs.update({f"gold_{key}": value for key, value in gold.logs.items()})
        baseline_f2p_failed = baseline.fail_to_pass_passed == 0
        baseline_p2p_passed = baseline.pass_to_pass_passed == len(oracle.pass_to_pass)
        gold_f2p_passed = gold.fail_to_pass_passed == len(oracle.fail_to_pass)
        gold_p2p_passed = gold.pass_to_pass_passed == len(oracle.pass_to_pass)
        if baseline.error_kind: errors.append(f"baseline: {baseline.error or baseline.error_kind}")
        if not baseline_f2p_failed: errors.append("baseline FAIL_TO_PASS tests unexpectedly pass")
        if not baseline_p2p_passed: errors.append("baseline PASS_TO_PASS tests fail")
        if gold.error_kind: errors.append(f"gold: {gold.error or gold.error_kind}")
        if not gold_f2p_passed: errors.append("gold patch does not pass FAIL_TO_PASS tests")
        if not gold_p2p_passed: errors.append("gold patch regresses PASS_TO_PASS tests")
        return InstanceValidationResult(
            instance_id=instance.instance_id,
            valid=not errors,
            baseline_fail_to_pass_failed=baseline_f2p_failed,
            baseline_pass_to_pass_passed=baseline_p2p_passed,
            gold_patch_applied=gold.patch_applied,
            gold_fail_to_pass_passed=gold_f2p_passed,
            gold_pass_to_pass_passed=gold_p2p_passed,
            errors=errors,
            logs=logs,
            environment_digest=digest,
            harness_version=self.name,
        )

    def _validate_baseline(self, instance: BenchmarkInstance, oracle: EvaluationOracle, parent: Path | None) -> CheckoutResult:
        result = CheckoutResult(patch_applied=True)
        with tempfile.TemporaryDirectory(prefix="sdd-eval-baseline-", dir=parent) as temporary:
            root, acquisition_log = self._prepare_checkout(instance, Path(temporary) / "repo")
            result.logs["checkout"] = acquisition_log
            if root is None:
                result.error = "repository checkout failed"; result.error_kind = "environment_error"; return result
            try:
                if not self._run_setup(root, instance, result): return result
            except ValueError as error:
                result.error = str(error); result.error_kind = "environment_error"; return result
            test_patch = self._apply_patch(root, oracle.test_patch, "test patch")
            result.logs["test_patch"] = test_patch.output
            if not test_patch.passed:
                result.error = "test patch could not be applied"; result.error_kind = "harness_error"; return result
            self._run_build_and_tests(root, instance, oracle, result)
        return result
