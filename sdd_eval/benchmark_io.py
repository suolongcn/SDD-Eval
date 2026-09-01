"""SWE-bench-compatible JSON/JSONL import and export helpers.

The default dataset export is public-safe: gold patches, test patches, and
test selectors are emitted only when ``include_oracle=True`` is explicitly
requested by an administrative caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import BenchmarkInstance, EvaluationOracle, Prediction, RequirementIR


def read_records(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError("benchmark JSON must contain an array")
        return payload
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _test_selectors(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return [str(item) for item in parsed] if isinstance(parsed, list) else [value]
    raise ValueError("test selectors must be a list or JSON-encoded list")


def from_swebench_record(
    record: dict[str, Any], *, dataset_id: str, dataset_version: str = "v1", split: str = "dev"
) -> tuple[BenchmarkInstance, EvaluationOracle]:
    instance_id = str(record.get("instance_id") or "").strip()
    repo = str(record.get("repo") or "").strip()
    base_commit = str(record.get("base_commit") or "").strip()
    problem = str(record.get("problem_statement") or "").strip()
    if not instance_id or not repo or not base_commit or not problem:
        raise ValueError("instance_id, repo, base_commit, and problem_statement are required")
    issue_url = record.get("issue_url")
    if not issue_url and "/" in repo and record.get("issue_id"):
        issue_url = f"https://github.com/{repo}/issues/{record['issue_id']}"
    instance = BenchmarkInstance(
        instance_id=instance_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        split=split,
        repo=repo,
        base_commit=base_commit,
        problem_statement=problem,
        language=str(record.get("language") or "python"),
        environment_id=record.get("environment_id"),
        version=str(record["version"]) if record.get("version") is not None else None,
        requirements=[RequirementIR(id="REQ-1", description=problem, source_refs=[str(issue_url)] if issue_url else [])],
        source_issue_url=str(issue_url) if issue_url else None,
        source_pr_url=str(record["pr_url"]) if record.get("pr_url") else None,
        difficulty=str(record["difficulty"]) if record.get("difficulty") else None,
    )
    oracle = EvaluationOracle(
        instance_id=instance_id,
        gold_patch=str(record.get("patch") or ""),
        test_patch=str(record.get("test_patch") or ""),
        fail_to_pass=_test_selectors(record.get("FAIL_TO_PASS")),
        pass_to_pass=_test_selectors(record.get("PASS_TO_PASS")),
        reference_commit=str(record["reference_commit"]) if record.get("reference_commit") else None,
    )
    return instance, oracle


def import_swebench(
    path: str | Path, *, dataset_id: str, dataset_version: str = "v1", split: str = "dev"
) -> list[tuple[BenchmarkInstance, EvaluationOracle]]:
    return [
        from_swebench_record(record, dataset_id=dataset_id, dataset_version=dataset_version, split=split)
        for record in read_records(path)
    ]


def _swebench_record(
    instance: BenchmarkInstance, oracle: EvaluationOracle | None, include_oracle: bool
) -> dict[str, Any]:
    record = {
        "instance_id": instance.instance_id,
        "repo": instance.repo,
        "base_commit": instance.base_commit,
        "problem_statement": instance.problem_statement,
        "version": instance.version,
        "created_at": instance.created_at.isoformat(),
        "issue_url": instance.source_issue_url,
        "pr_url": instance.source_pr_url,
        "difficulty": instance.difficulty,
        "language": instance.language,
    }
    if include_oracle:
        if oracle is None:
            raise ValueError(f"missing oracle for {instance.instance_id}")
        record.update({
            "patch": oracle.gold_patch,
            "test_patch": oracle.test_patch,
            "FAIL_TO_PASS": json.dumps(oracle.fail_to_pass),
            "PASS_TO_PASS": json.dumps(oracle.pass_to_pass),
        })
    return record


def export_swebench(
    path: str | Path,
    instances: Iterable[BenchmarkInstance],
    *,
    oracles: dict[str, EvaluationOracle] | None = None,
    include_oracle: bool = False,
) -> None:
    oracle_map = oracles or {}
    lines = [
        json.dumps(_swebench_record(instance, oracle_map.get(instance.instance_id), include_oracle), ensure_ascii=False)
        for instance in instances
    ]
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def export_predictions(path: str | Path, predictions: Iterable[Prediction]) -> None:
    lines = [
        json.dumps({
            "instance_id": prediction.instance_id,
            "model_name_or_path": prediction.model_name_or_path,
            "model_patch": prediction.model_patch,
        }, ensure_ascii=False)
        for prediction in predictions
    ]
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
