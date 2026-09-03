from pathlib import Path
import subprocess
import sys

from sdd_eval.harness import LocalEvaluationBackend
from sdd_eval.models import BenchmarkInstance, EnvironmentSpec, EvaluationOracle, Prediction


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout


def patch_for(root: Path, changes: dict[str, str]) -> str:
    tracked = set(git(root, "ls-files").splitlines())
    for relative, content in changes.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if relative not in tracked:
            git(root, "add", "-N", relative)
    patch = git(root, "diff", "--binary", "HEAD")
    git(root, "reset", "--hard", "HEAD")
    git(root, "clean", "-fd")
    return patch


def benchmark_fixture(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (repo / "existing.txt").write_text("stable\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    base_commit = git(repo, "rev-parse", "HEAD").strip()
    gold_patch = patch_for(repo, {"app.py": "def value():\n    return 2\n"})
    target_failed_patch = patch_for(repo, {"app.py": "def value():\n    return 3\n"})
    regression_patch = patch_for(repo, {
        "app.py": "def value():\n    return 2\n",
        "existing.txt": "broken\n",
    })
    test_runner = """import sys
from pathlib import Path
import app

selector = sys.argv[1]
if selector == "test_bug":
    assert app.value() == 2
elif selector == "test_existing":
    assert Path("existing.txt").read_text(encoding="utf-8") == "stable\\n"
else:
    raise AssertionError(f"unknown selector: {selector}")
"""
    test_patch = patch_for(repo, {"test_runner.py": test_runner})
    instance = BenchmarkInstance(
        instance_id="demo__repo-1",
        repo=str(repo),
        base_commit=base_commit,
        problem_statement="Make value return 2.",
        environment=EnvironmentSpec(
            build_command=[sys.executable, "-m", "py_compile", "app.py"],
            test_command=[sys.executable, "test_runner.py", "{tests}"],
        ),
    )
    oracle = EvaluationOracle(
        instance_id=instance.instance_id,
        gold_patch=gold_patch,
        test_patch=test_patch,
        fail_to_pass=["test_bug"],
        pass_to_pass=["test_existing"],
    )
    return instance, oracle, gold_patch, target_failed_patch, regression_patch


def prediction(instance_id: str, patch: str, prediction_id: str = "prediction-1") -> Prediction:
    return Prediction(
        prediction_id=prediction_id,
        instance_id=instance_id,
        model_name_or_path="test-model",
        model_patch=patch,
    )


def test_test_selector_placeholder_can_be_embedded_in_an_argument():
    command = LocalEvaluationBackend()._expand_test_command(
        ["mvnw.cmd", "-q", "-Dtest={tests}", "test"],
        ["HealthEndpointOracleTest#healthEndpointReturnsOk"],
    )
    assert command == ["mvnw.cmd", "-q", "-Dtest=HealthEndpointOracleTest#healthEndpointReturnsOk", "test"]


def test_apply_patch_accepts_lf_diff_for_lf_checkout_on_windows(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"first\nsecond\n")
    patch = """diff --git a/source.txt b/source.txt
index 66a52ee..75d8e14 100644
--- a/source.txt
+++ b/source.txt
@@ -1,2 +1,2 @@
 first
-second
+changed
"""

    result = LocalEvaluationBackend()._apply_patch(tmp_path, patch, "test patch")

    assert result.passed, result.output
    assert source.read_text(encoding="utf-8").splitlines() == ["first", "changed"]


def test_gold_validation_and_prediction_resolve(tmp_path):
    instance, oracle, gold_patch, _, _ = benchmark_fixture(tmp_path)
    backend = LocalEvaluationBackend()

    validation = backend.validate_instance(instance, oracle, workspace=tmp_path / "work")
    result = backend.evaluate(instance, oracle, prediction(instance.instance_id, gold_patch), workspace=tmp_path / "work")

    assert validation.valid
    assert validation.baseline_fail_to_pass_failed
    assert validation.baseline_pass_to_pass_passed
    assert validation.gold_fail_to_pass_passed
    assert validation.gold_pass_to_pass_passed
    assert result.outcome == "resolved" and result.resolved
    assert result.score == 100
    assert result.fail_to_pass_passed == result.fail_to_pass_total == 1
    assert result.pass_to_pass_passed == result.pass_to_pass_total == 1


def test_target_failure_and_regression_are_distinct(tmp_path):
    instance, oracle, _, target_failed_patch, regression_patch = benchmark_fixture(tmp_path)
    backend = LocalEvaluationBackend()

    target_failed = backend.evaluate(instance, oracle, prediction(instance.instance_id, target_failed_patch, "target"))
    regression = backend.evaluate(instance, oracle, prediction(instance.instance_id, regression_patch, "regression"))

    assert target_failed.outcome == "target_tests_failed"
    assert target_failed.fail_to_pass_passed == 0
    assert target_failed.score == 50
    assert regression.outcome == "regression"
    assert regression.fail_to_pass_passed == 1
    assert regression.pass_to_pass_passed == 0
    assert regression.score == 50


def test_composite_score_weights_functional_code_and_documentation(tmp_path):
    instance, oracle, gold_patch, _, _ = benchmark_fixture(tmp_path)
    prediction_value = prediction(instance.instance_id, gold_patch, "weighted")
    prediction_value.artifacts.documents = {"spec.md": "requirements", "design.md": "design"}
    result = LocalEvaluationBackend().evaluate(instance, oracle, prediction_value)

    assert result.functional_score == 100
    assert result.code_quality_score == 100
    assert result.documentation_score == 80
    assert result.score == 95
    assert result.score_weights == {"functional": 0.5, "code_quality": 0.25, "documentation": 0.25}


def test_configured_style_and_coverage_commands_affect_code_quality(tmp_path):
    instance, oracle, gold_patch, _, _ = benchmark_fixture(tmp_path)
    oracle.quality_review = {
        "style_command": [sys.executable, "-c", "import sys; print('lint violation'); sys.exit(1)"],
        "coverage_command": [sys.executable, "-c", "print('COVERAGE: 60%')"],
        "coverage_threshold": 80,
    }

    result = LocalEvaluationBackend().evaluate(instance, oracle, prediction(instance.instance_id, gold_patch))

    assert result.outcome == "resolved"
    assert result.code_quality_score == 58.33
    assert result.code_quality_metrics["command_checks"]["style"]["status"] == "failed"
    assert result.code_quality_metrics["command_checks"]["coverage"]["status"] == "below_threshold"
    assert {item["check_id"] for item in result.quality_findings} >= {"code_style", "test_coverage"}


def test_empty_and_forbidden_patches_are_invalid(tmp_path):
    instance, oracle, gold_patch, _, regression_patch = benchmark_fixture(tmp_path)
    backend = LocalEvaluationBackend()

    empty = backend.evaluate(instance, oracle, prediction(instance.instance_id, "", "empty"))
    forbidden_oracle = oracle.model_copy(update={"forbidden_paths": ["existing.txt"]})
    forbidden = backend.evaluate(instance, forbidden_oracle, prediction(instance.instance_id, regression_patch, "forbidden"))

    assert empty.outcome == "invalid_patch" and not empty.patch_applied
    assert forbidden.outcome == "invalid_patch"
    assert forbidden.functional_metrics["forbidden_changes"] == ["existing.txt"]
    assert gold_patch


def test_new_files_are_checked_against_forbidden_paths(tmp_path):
    instance, oracle, _, _, _ = benchmark_fixture(tmp_path)
    new_file_patch = patch_for(Path(instance.repo), {
        "hidden/answer.py": "SECRET = True\n",
        "app.py": "def value():\n    return 2\n",
    })
    protected = oracle.model_copy(update={"forbidden_paths": ["hidden/**"]})

    result = LocalEvaluationBackend().evaluate(
        instance, protected, prediction(instance.instance_id, new_file_patch, "new-file")
    )

    assert result.outcome == "invalid_patch"
    assert result.functional_metrics["forbidden_changes"] == ["hidden/answer.py"]
