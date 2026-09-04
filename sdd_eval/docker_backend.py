"""Docker-backed executable-oracle grading for Benchmark V2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import uuid

from .harness import CheckoutResult, CommandResult, LocalEvaluationBackend
from .models import BenchmarkInstance, EvaluationOracle, Prediction
from .quality import command_quality_metrics, quality_command_policy


DOCKER_HARNESS_VERSION = "docker-v1"


class DockerEvaluationBackend(LocalEvaluationBackend):
    """Grade trusted predictions inside a resource-limited Docker container."""

    name = DOCKER_HARNESS_VERSION

    def __init__(self, docker_executable: str | list[str] = "docker"):
        self.docker_executable = docker_executable
        self._image_digests: dict[str, str] = {}

    def _docker(self, arguments: list[str], timeout: int = 600) -> CommandResult:
        if isinstance(self.docker_executable, list):
            command = [*self.docker_executable, *arguments]
        else:
            executable = shutil.which(self.docker_executable) or self.docker_executable
            command = [executable, *arguments]
        return self._run(command, Path.cwd(), timeout)

    @classmethod
    def for_host(cls) -> "DockerEvaluationBackend":
        """Use native Docker, or the default WSL distribution on Windows."""
        if shutil.which("docker") or shutil.which("docker.exe"):
            return cls()
        if shutil.which("wsl.exe"):
            probe = subprocess.run(
                ["wsl.exe", "sh", "-lc", "command -v docker >/dev/null && docker info >/dev/null 2>&1"],
                capture_output=True, timeout=30,
            )
            if probe.returncode == 0:
                return cls(["wsl.exe", "docker"])
        return cls()

    def available(self) -> bool:
        return self._docker(["info", "--format", "{{.ServerVersion}}"], 20).passed

    def _image_digest(self, image: str) -> str:
        if image in self._image_digests:
            return self._image_digests[image]
        inspect = self._docker(["image", "inspect", image, "--format", "{{.Id}}"], 60)
        digest = inspect.output.strip() if inspect.passed else "unavailable"
        if inspect.passed:
            self._image_digests[image] = digest
        return digest

    def _ensure_image(self, instance: BenchmarkInstance) -> CommandResult:
        spec = instance.docker
        if not spec.image:
            return CommandResult(False, 1, "docker.image is required")
        inspect = self._docker(["image", "inspect", spec.image], 60)
        if inspect.passed:
            return CommandResult(True, 0, "using cached image")
        if spec.build_context:
            arguments = ["build", "--tag", spec.image]
            if spec.platform:
                arguments.extend(["--platform", spec.platform])
            if spec.dockerfile:
                arguments.extend(["--file", spec.dockerfile])
            arguments.append(spec.build_context)
            built = self._docker(arguments, 1800)
            if built.passed:
                self._image_digests.pop(spec.image, None)
            return built
        if spec.pull:
            pulled = self._docker(["pull", spec.image], 900)
            if pulled.passed:
                self._image_digests.pop(spec.image, None)
            return pulled
        return CommandResult(False, 1, f"Docker image is unavailable: {spec.image}")

    def execution_manifest(self, instance: BenchmarkInstance) -> dict:
        image = instance.docker.image or ""
        return {
            "backend": self.name,
            "image": image,
            "image_digest": self._image_digest(image) if image else "",
            "build_context": instance.docker.build_context,
            "dockerfile": instance.docker.dockerfile,
            "platform": instance.docker.platform,
            "setup_network": instance.docker.setup_network,
            "grading_network_disabled": instance.docker.grading_network_disabled,
            "read_only_root": instance.docker.read_only_root,
            "limits": instance.docker.limits.model_dump(mode="json"),
        }

    def environment_digest(self, instance: BenchmarkInstance) -> str:
        payload = {
            "manifest": self.execution_manifest(instance),
            "environment_id": instance.environment_id,
            "language": instance.language,
            "environment": instance.environment.model_dump(mode="json"),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def create_command(self, instance: BenchmarkInstance, checkout: Path, container_name: str) -> list[str]:
        spec, limits = instance.docker, instance.docker.limits
        if not spec.image:
            raise ValueError("docker.image is required")
        mount_source = str(checkout.resolve())
        if isinstance(self.docker_executable, list) and self.docker_executable[:2] == ["wsl.exe", "docker"]:
            drive, tail = Path(mount_source).drive.rstrip(":"), Path(mount_source).parts[1:]
            if drive:
                mount_source = "/mnt/" + drive.lower() + "/" + "/".join(tail)
        command = [
            "create", "--name", container_name,
            "--network", spec.setup_network,
            "--cpus", str(limits.cpus),
            "--memory", f"{limits.memory_mb}m",
            "--pids-limit", str(limits.pids_limit),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_mb}m",
            "--env", "HOME=/tmp/home",
            "--env", "XDG_CACHE_HOME=/tmp/cache",
            "--mount", f"type=bind,src={mount_source},dst=/workspace",
        ]
        if spec.dependency_cache_key:
            cache_id = hashlib.sha256(spec.dependency_cache_key.encode("utf-8")).hexdigest()[:16]
            command.extend(["--mount", f"type=volume,source=sdd-eval-cache-{cache_id},target=/sdd-cache"])
        if spec.read_only_root:
            command.append("--read-only")
        if spec.platform:
            command.extend(["--platform", spec.platform])
        if spec.user:
            command.extend(["--user", spec.user])
        command.extend([spec.image, "sh", "-c", "trap : TERM INT; sleep infinity & wait"])
        return command

    def _container_cwd(self, instance: BenchmarkInstance) -> str:
        relative = PurePosixPath(instance.environment.working_directory.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("working_directory escapes the repository")
        return str(PurePosixPath("/workspace") / relative)

    def _exec(self, container: str, cwd: str, command: list[str], timeout: int) -> CommandResult:
        return self._docker(["exec", "--workdir", cwd, container, *command], timeout)

    def _disconnect_for_grading(self, instance: BenchmarkInstance, container: str) -> CommandResult:
        network = instance.docker.setup_network
        if not instance.docker.grading_network_disabled or network == "none":
            return CommandResult(True, 0, "grading network already disabled or explicitly allowed")
        return self._docker(["network", "disconnect", "--force", network, container], 60)

    def _run_in_container(
        self, instance: BenchmarkInstance, oracle: EvaluationOracle,
        model_patch: str | None, run_root: Path,
    ) -> CheckoutResult:
        result = CheckoutResult(patch_applied=model_patch is None)
        root, acquisition_log = self._prepare_checkout(instance, run_root / "repo")
        result.logs["checkout"] = acquisition_log
        if root is None:
            result.error = "repository checkout failed"; result.error_kind = "environment_error"; return result
        try:
            self._working_directory(root, instance)
            container_cwd = self._container_cwd(instance)
        except ValueError as error:
            result.error = str(error); result.error_kind = "environment_error"; return result
        if model_patch is not None:
            patch = self._apply_patch(root, model_patch, "model patch")
            result.logs["model_patch"] = patch.output
            if not patch.passed:
                result.error = "model patch could not be applied"; result.error_kind = "invalid_patch"; return result
            result.patch_applied = True
            result.forbidden_changes = self._forbidden_changes(self._changed_paths(root), oracle.forbidden_paths)
            if result.forbidden_changes:
                result.error = "model patch modifies forbidden paths"; result.error_kind = "invalid_patch"; return result
            self._run_code_quality(root, oracle, model_patch, result)
        test_patch = self._apply_patch(root, oracle.test_patch, "test patch")
        result.logs["test_patch"] = test_patch.output
        if not test_patch.passed:
            result.error = "test patch could not be applied"; result.error_kind = "harness_error"; return result
        if not self.available():
            result.error = "Docker CLI or daemon is unavailable"; result.error_kind = "environment_error"; return result
        if not instance.docker.image:
            result.error = "docker.image is required"; result.error_kind = "environment_error"; return result
        image = self._ensure_image(instance)
        result.logs["image_prepare"] = image.output
        if not image.passed:
            result.error = "Docker image preparation failed"; result.error_kind = "environment_error"; return result
        container = "sdd-eval-" + re.sub(r"[^a-z0-9_.-]", "-", uuid.uuid4().hex[:12].lower())
        try:
            create = self._docker(self.create_command(instance, root, container), 180)
            result.logs["container_create"] = create.output
            if not create.passed:
                result.error = "container creation failed"; result.error_kind = "environment_error"; return result
            start = self._docker(["start", container], 60)
            result.logs["container_start"] = start.output
            if not start.passed:
                result.error = "container start failed"; result.error_kind = "environment_error"; return result
            for index, command in enumerate(instance.environment.setup_commands, start=1):
                setup = self._exec(container, container_cwd, command, instance.environment.setup_timeout_seconds)
                result.logs[f"setup_{index}"] = setup.output
                if not setup.passed:
                    result.error = f"setup command {index} failed"; result.error_kind = "environment_error"; return result
            disconnect = self._disconnect_for_grading(instance, container)
            result.logs["grading_network"] = disconnect.output
            if not disconnect.passed:
                result.error = "could not disable grading network"; result.error_kind = "harness_error"; return result
            if instance.environment.build_command:
                build = self._exec(container, container_cwd, instance.environment.build_command, instance.environment.build_timeout_seconds)
                result.logs["build"] = build.output
                result.build_passed = build.passed
                if not build.passed:
                    result.error = "build command failed"; result.error_kind = "build_failed"; return result
            else:
                result.build_passed = True
            for group_name, selectors in (("fail_to_pass", oracle.fail_to_pass), ("pass_to_pass", oracle.pass_to_pass)):
                passed, outputs, cases = 0, [], []
                for selector in selectors:
                    command = self._expand_test_command(instance.environment.test_command, [selector])
                    test = self._exec(container, container_cwd, command, instance.environment.test_timeout_seconds)
                    outputs.append(f"===== {selector} (exit {test.returncode}) =====\n{test.output}")
                    cases.append({
                        "selector": selector,
                        "passed": test.passed,
                        "returncode": test.returncode,
                        "output": test.output,
                    })
                    passed += int(test.passed)
                result.logs[group_name] = "\n".join(outputs)
                result.test_cases[group_name] = cases
                if group_name == "fail_to_pass": result.fail_to_pass_passed = passed
                else: result.pass_to_pass_passed = passed
            policy = quality_command_policy(oracle)
            timeout = policy["quality_timeout_seconds"]
            style_command, coverage_command = policy.get("style_command"), policy.get("coverage_command")
            style = self._exec(container, container_cwd, style_command, timeout) if style_command else None
            coverage = self._exec(container, container_cwd, coverage_command, timeout) if coverage_command else None
            metrics, findings = command_quality_metrics(
                style_returncode=style.returncode if style else None,
                style_output=style.output if style else "",
                coverage_returncode=coverage.returncode if coverage else None,
                coverage_output=coverage.output if coverage else "",
                coverage_threshold=policy["coverage_threshold"],
            )
            result.code_quality_metrics["command_checks"] = metrics
            result.quality_findings.extend(item.as_dict() for item in findings)
            if style:
                result.logs["code_style"] = style.output
            if coverage:
                result.logs["test_coverage"] = coverage.output
            return result
        finally:
            cleanup = self._docker(["rm", "--force", container], 60)
            result.logs["container_cleanup"] = cleanup.output

    def _execute_patch(self, instance: BenchmarkInstance, oracle: EvaluationOracle, model_patch: str, run_root: Path) -> CheckoutResult:
        return self._run_in_container(instance, oracle, model_patch, run_root)

    def _validate_baseline(self, instance: BenchmarkInstance, oracle: EvaluationOracle, parent: Path | None) -> CheckoutResult:
        with tempfile.TemporaryDirectory(prefix="sdd-eval-docker-baseline-", dir=parent) as temporary:
            return self._run_in_container(instance, oracle, None, Path(temporary))

    def evaluate(self, instance: BenchmarkInstance, oracle: EvaluationOracle, prediction: Prediction, workspace=None):
        result = super().evaluate(instance, oracle, prediction, workspace=workspace)
        result.execution_manifest = self.execution_manifest(instance)
        return result
