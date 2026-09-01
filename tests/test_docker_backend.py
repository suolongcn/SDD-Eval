from pathlib import Path
import subprocess

from sdd_eval.docker_backend import DockerEvaluationBackend
from sdd_eval.harness import CommandResult
from sdd_eval.models import BenchmarkInstance, ContainerLimits, DockerSpec, EnvironmentSpec, EvaluationOracle, Prediction


def docker_instance(repo: str = ".") -> BenchmarkInstance:
    return BenchmarkInstance(
        instance_id="demo__repo-1",
        repo=repo,
        base_commit="abc123",
        problem_statement="Fix it.",
        environment=EnvironmentSpec(
            build_command=["python", "-m", "compileall", "-q", "."],
            test_command=["python", "test_runner.py", "{tests}"],
        ),
        docker=DockerSpec(
            image="python:3.12-alpine",
            platform="linux/amd64",
            setup_network="bridge",
            grading_network_disabled=True,
            read_only_root=True,
            user="1000:1000",
            limits=ContainerLimits(cpus=1.5, memory_mb=768, pids_limit=128, tmpfs_mb=64),
        ),
    )


def test_container_command_enforces_security_and_resource_limits(tmp_path):
    instance = docker_instance()
    command = DockerEvaluationBackend().create_command(instance, tmp_path, "sdd-eval-test")

    assert command[:3] == ["create", "--name", "sdd-eval-test"]
    assert command[command.index("--network") + 1] == "bridge"
    assert command[command.index("--cpus") + 1] == "1.5"
    assert command[command.index("--memory") + 1] == "768m"
    assert command[command.index("--pids-limit") + 1] == "128"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert "--read-only" in command
    assert command[command.index("--user") + 1] == "1000:1000"
    assert "dst=/workspace" in command[command.index("--mount") + 1]


def test_grading_network_is_disconnected_unless_explicitly_allowed(monkeypatch):
    backend = DockerEvaluationBackend()
    calls = []
    monkeypatch.setattr(backend, "_docker", lambda args, timeout=600: calls.append(args) or __import__("sdd_eval.harness", fromlist=["CommandResult"]).CommandResult(True, 0, "ok"))
    instance = docker_instance()

    assert backend._disconnect_for_grading(instance, "container").passed
    assert calls == [["network", "disconnect", "--force", "bridge", "container"]]

    calls.clear()
    allowed = instance.model_copy(update={"docker": instance.docker.model_copy(update={"grading_network_disabled": False})})
    assert backend._disconnect_for_grading(allowed, "container").passed
    assert calls == []


def test_missing_docker_returns_an_environment_error(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout
    git("init"); git("config", "user.email", "test@example.com"); git("config", "user.name", "Test")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "test_runner.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    git("add", "."); git("commit", "-m", "base")
    base_commit = git("rev-parse", "HEAD").strip()
    (repo / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    gold_patch = git("diff", "HEAD"); git("reset", "--hard", "HEAD")
    (repo / "test_runner.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    test_patch = git("diff", "HEAD"); git("reset", "--hard", "HEAD")
    instance = docker_instance(str(repo)).model_copy(update={
        "base_commit": base_commit,
        "docker": DockerSpec(image="local/test:missing"),
    })
    oracle = EvaluationOracle(instance_id=instance.instance_id, gold_patch=gold_patch, test_patch=test_patch, fail_to_pass=["test_bug"])
    prediction = Prediction(instance_id=instance.instance_id, model_name_or_path="test", model_patch=gold_patch)
    result = DockerEvaluationBackend(docker_executable="definitely-not-a-docker-command").evaluate(
        instance, oracle, prediction
    )

    assert result.outcome == "environment_error"
    assert result.functional_metrics["error"] == "Docker CLI or daemon is unavailable"
    assert result.execution_manifest["backend"] == "docker-v1"
    assert result.execution_manifest["image"] == "local/test:missing"


def test_working_directory_cannot_escape_container_workspace():
    backend = DockerEvaluationBackend()
    instance = docker_instance().model_copy(update={
        "environment": EnvironmentSpec(working_directory="../outside")
    })

    try:
        backend._container_cwd(instance)
    except ValueError as error:
        assert "escapes" in str(error)
    else:
        raise AssertionError("escaping working_directory was accepted")


def test_docker_validation_records_docker_harness_version():
    instance = docker_instance()
    oracle = EvaluationOracle(instance_id=instance.instance_id)

    validation = DockerEvaluationBackend(docker_executable="definitely-not-a-docker-command").validate_instance(instance, oracle)

    assert not validation.valid
    assert validation.harness_version == "docker-v1"


def test_missing_image_can_be_built_from_admin_context(monkeypatch):
    backend = DockerEvaluationBackend()
    instance = docker_instance().model_copy(update={
        "docker": DockerSpec(image="sdd/test:local", build_context="docker/context", dockerfile="Dockerfile.eval")
    })
    calls = []
    def fake_docker(args, timeout=600):
        calls.append(args)
        return CommandResult(False, 1, "missing") if args[:2] == ["image", "inspect"] else CommandResult(True, 0, "built")
    monkeypatch.setattr(backend, "_docker", fake_docker)

    result = backend._ensure_image(instance)

    assert result.passed
    assert calls[1] == [
        "build", "--tag", "sdd/test:local", "--file", "Dockerfile.eval", "docker/context"
    ]


def test_container_is_removed_when_start_fails(tmp_path, monkeypatch):
    backend = DockerEvaluationBackend()
    instance = docker_instance()
    oracle = EvaluationOracle(instance_id=instance.instance_id, test_patch="test patch", fail_to_pass=["test_bug"])
    checkout = tmp_path / "checkout"; checkout.mkdir()
    calls = []
    monkeypatch.setattr(backend, "_prepare_checkout", lambda instance, destination: (checkout, "checked out"))
    monkeypatch.setattr(backend, "_apply_patch", lambda root, patch, label: CommandResult(True, 0, "applied"))
    monkeypatch.setattr(backend, "_changed_paths", lambda root: [])
    monkeypatch.setattr(backend, "available", lambda: True)
    monkeypatch.setattr(backend, "_ensure_image", lambda instance: CommandResult(True, 0, "cached"))
    monkeypatch.setattr(backend, "create_command", lambda instance, root, name: ["create", name])
    def fake_docker(args, timeout=600):
        calls.append(args)
        if args[0] == "start": return CommandResult(False, 1, "start failed")
        return CommandResult(True, 0, "ok")
    monkeypatch.setattr(backend, "_docker", fake_docker)

    result = backend._run_in_container(instance, oracle, "model patch", tmp_path / "run")

    assert result.error_kind == "environment_error"
    assert calls[-1][0:2] == ["rm", "--force"]
