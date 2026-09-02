"""Seed ten issue/PR-backed benchmarks from merged GitHub pull requests.

The public Instance stores the upstream PR metadata and exact final diff line
count. The private Oracle stores the upstream diff plus a small hidden smoke
test that distinguishes the PR base from the merged implementation without
requiring each project's full dependency toolchain.
"""

from __future__ import annotations

from pathlib import Path
import random
import time
import urllib.error
import urllib.request

from sdd_eval.models import (
    ArtifactBundle,
    BenchmarkInstance,
    EnvironmentSpec,
    EvaluationOracle,
    Prediction,
    RequirementIR,
    TraceLink,
)
from sdd_eval.storage import Store


WORKSPACE = Path(__file__).resolve().parents[1]
DATASET_ID = "github-merged-prs"
DATASET_VERSION = "2026-09-01"


TASKS = [
    {
        "repo": "iluwatar/java-design-patterns",
        "pr": 3582,
        "issue": 3576,
        "base_commit": "22a34127d0b08449c24cf7e230c04a097deca2f3",
        "reference_commit": "e604256708ff66a6d44ce9c3a23d4d933e657d78",
        "lines": 1116,
        "language": "java",
        "problem": "Implement a self-contained Write-Ahead Log design-pattern example with durable append, read, and recovery behavior and focused tests.",
    },
    {
        "repo": "fastapi/fastapi",
        "pr": 16049,
        "base_commit": "a64dfbbd21a445288ff583d58e1f646fe6baf3af",
        "reference_commit": "0b81a548eab6b22848134261bdd45103257eeaa4",
        "lines": 614,
        "language": "python",
        "problem": "Reduce dependency graph memory usage by making dependency models more compact and safely reusing cached dependency information.",
    },
    {
        "repo": "pallets/flask",
        "pr": 5812,
        "base_commit": "330123258e8c3dc391cbe55ab1ed94891ca83af3",
        "reference_commit": "c2705ffd9ce1dc8476cb29eaf5ff5d4c719852d9",
        "lines": 1774,
        "language": "python",
        "problem": "Merge Flask application and request context handling while preserving lifecycle, session, signal, testing, and extension behavior.",
    },
    {
        "repo": "pydantic/pydantic",
        "pr": 13725,
        "base_commit": "c5af602863565eac3e1e4e2ed4aafe9ade8fdd3e",
        "reference_commit": "849ab46a67849f8f99af2d20736de246eead7960",
        "lines": 957,
        "language": "python-rust",
        "problem": "Move Pydantic schema gathering and cleaning logic into pydantic-core while preserving schema traversal and definition behavior.",
    },
    {
        "repo": "pallets/jinja",
        "pr": 2096,
        "base_commit": "05f5d74849f8b65d28e1f36e349c2da10061d413",
        "reference_commit": "ece7c271f3c5df13eb43da3badd6f069e0542f55",
        "lines": 1039,
        "language": "python",
        "problem": "Drop end-of-life Python versions and modernize Jinja's supported-version metadata and type annotations consistently.",
    },
    {
        "repo": "celery/celery",
        "pr": 10504,
        "base_commit": "8d2bccca0478cad48f31a75eaebc0ce389f65425",
        "reference_commit": "3db60302a115bb79aa9abc4d50470d421fd15185",
        "lines": 519,
        "language": "python",
        "problem": "Make result_compression actually compress values stored by result backends, including backend-specific and regression coverage.",
    },
    {
        "repo": "spring-projects/spring-petclinic",
        "pr": 2611,
        "issue": 2600,
        "base_commit": "88e37c15cf6fc8490b01bc3e8e2c800cec1ac272",
        "reference_commit": "c9d051dcd3769c323208d366ee21614b913f96e1",
        "lines": 81,
        "language": "java",
        "problem": "Fix Owner.addPet for persisted pets and reject pet names longer than the supported maximum, with regression tests for both behaviors.",
    },
    {
        "repo": "django/django",
        "pr": 21754,
        "base_commit": "73cc09f14f13fedddc14d6ba5b287cb33c24e4a4",
        "reference_commit": "1a180c837c27206932cd6abc14d3ef654390d9a9",
        "lines": 226,
        "language": "python",
        "problem": "Fix Django admin changelist __exact searches that crash or over-match, including relations and empty-value cases.",
    },
    {
        "repo": "django/django",
        "pr": 21745,
        "base_commit": "0b40210e4808937a7c0922e8b7502bff4752faa3",
        "reference_commit": "019551708027e70ddaea5910276493b5a4b30f0c",
        "lines": 246,
        "language": "python",
        "problem": "Remove reliance on implicit SELECT ordering throughout Django's test suite so assertions remain deterministic across databases.",
    },
    {
        "repo": "pallets/flask",
        "pr": 5928,
        "base_commit": "7b0088693ece1bd3a9238a6fdf56ed8df7a4d43b",
        "reference_commit": "fbb6f0bc4c60a0bada0e03c3480d0ccf30a3c1df",
        "lines": 274,
        "language": "python",
        "problem": "Ensure every Flask teardown callback is invoked even when an earlier teardown callback raises an exception.",
    },
]


def fetch_pr_diff(repo: str, pr: int) -> str:
    """Fetch GitHub's final PR diff, retrying transient rate limits."""
    url = f"https://github.com/{repo}/pull/{pr}.diff"
    for attempt in range(5):
        request = urllib.request.Request(
            f"{url}?seed={random.randrange(1_000_000_000)}",
            headers={"User-Agent": "SDD-Eval-Benchmark-Seeder/2.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 4:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"unable to fetch {url}")


def changed_lines(patch: str) -> int:
    return sum(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in patch.splitlines()
    )


def smoke_anchor(patch: str) -> tuple[str, str]:
    """Select a stable added source line for the hidden base-vs-PR check."""
    path = ""
    additions: list[tuple[int, str, str]] = []
    for line in patch.splitlines():
        if line.startswith("diff --git a/"):
            path = line[len("diff --git a/"):].split(" b/", 1)[0]
            continue
        if not path or not line.startswith("+") or line.startswith("+++"):
            continue
        value = line[1:]
        stripped = value.strip()
        if len(stripped) < 16 or stripped.startswith(("#", "//", "*")):
            continue
        normalized = path.lower()
        penalty = 0
        if any(part in normalized for part in ("test", "docs/", "readme", ".github", "lock")):
            penalty += 10_000
        if normalized.endswith((".md", ".rst", ".txt", ".toml", ".yml", ".yaml")):
            penalty += 5_000
        additions.append((penalty - min(len(stripped), 500), path, value))
    if not additions:
        raise ValueError("PR diff has no suitable added line for a smoke Oracle")
    _, selected_path, selected_line = min(additions)
    return selected_path, selected_line


def added_file_patch(path: str, content: str) -> str:
    lines = content.splitlines()
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def oracle_test_patch(task: dict, patch: str) -> tuple[str, str, str, str]:
    target_path, marker = smoke_anchor(patch)
    slug = f"pr_{task['pr']}"
    fail_path = f".sdd_eval_tests/{slug}_fail.py"
    pass_path = f".sdd_eval_tests/{slug}_pass.py"
    fail_content = "\n".join([
        "from pathlib import Path",
        f"target = Path({target_path!r})",
        f"marker = {marker!r}",
        "assert target.is_file(), f'expected PR file is missing: {target}'",
        "text = target.read_text(encoding='utf-8', errors='replace')",
        "assert marker in text, f'official PR marker is missing from {target}'",
    ]) + "\n"
    pass_content = "\n".join([
        "from pathlib import Path",
        "assert Path('.git').is_dir(), 'benchmark checkout is not a Git repository'",
    ]) + "\n"
    return (
        added_file_patch(fail_path, fail_content) + added_file_patch(pass_path, pass_content),
        fail_path,
        pass_path,
        target_path,
    )


def main() -> None:
    store = Store(str(WORKSPACE / "sdd_eval.db"))
    for task in TASKS:
        repo = task["repo"]
        pr = task["pr"]
        pr_url = f"https://github.com/{repo}/pull/{pr}"
        issue_url = f"https://github.com/{repo}/issues/{task['issue']}" if task.get("issue") else None
        patch = fetch_pr_diff(repo, pr)
        actual_lines = changed_lines(patch)
        if actual_lines != task["lines"]:
            raise ValueError(f"{repo}#{pr}: expected {task['lines']} changed lines, got {actual_lines}")
        test_patch, fail_test, pass_test, target_path = oracle_test_patch(task, patch)
        instance_id = f"{repo.replace('/', '__')}__pr-{pr}"
        requirement = RequirementIR(
            id="REQ-1",
            description=task["problem"],
            kind="functional",
            acceptance_criteria=["The behavior and regression coverage of the merged upstream PR are preserved."],
            source_refs=[value for value in (issue_url, pr_url) if value],
            oracle_refs=[target_path],
        )
        instance = BenchmarkInstance(
            instance_id=instance_id,
            dataset_id=DATASET_ID,
            dataset_version=DATASET_VERSION,
            split="verified",
            repo=f"https://github.com/{repo}.git",
            base_commit=task["base_commit"],
            problem_statement=task["problem"],
            language=task["language"],
            environment=EnvironmentSpec(
                test_command=["python", "{tests}"],
                working_directory=".",
                test_timeout_seconds=120,
            ),
            requirements=[requirement],
            constraints=["Preserve behavior outside the merged PR scope", "Do not modify hidden Oracle tests"],
            source_issue_url=issue_url,
            source_pr_url=pr_url,
            reference_code_lines=actual_lines,
            reference_code_estimated=False,
            difficulty="large" if actual_lines >= 500 else "small",
        )
        oracle = EvaluationOracle(
            instance_id=instance_id,
            gold_patch=patch,
            test_patch=test_patch,
            fail_to_pass=[fail_test],
            pass_to_pass=[pass_test],
            forbidden_paths=[".sdd_eval_tests/**"],
            reference_commit=task["reference_commit"],
            quality_review={
                "source": "merged-github-pr",
                "source_pr_url": pr_url,
                "source_issue_url": issue_url,
                "source_base_commit": task["base_commit"],
                "exact_changed_lines": actual_lines,
                "line_count_method": "final PR diff additions plus deletions",
                "smoke_anchor_path": target_path,
            },
        )
        prediction = Prediction(
            prediction_id=f"gold-{repo.replace('/', '-')}-{pr}",
            instance_id=instance_id,
            model_name_or_path="official-merged-pr",
            client="fixture",
            workflow="reference",
            model_patch=patch,
            artifacts=ArtifactBundle(
                documents={"requirement.md": task["problem"]},
                trace_links=[TraceLink(
                    source_type="requirement",
                    source_id="REQ-1",
                    target_type="code",
                    target_id=target_path,
                    status="covered",
                    evidence=[pr_url],
                )],
            ),
        )
        store.delete_benchmark_instance(instance_id)
        store.put_benchmark_instance(instance, oracle)
        store.put_prediction(prediction)
        print(f"seeded {instance_id}: {actual_lines} lines -> {pr_url}")


if __name__ == "__main__":
    main()
