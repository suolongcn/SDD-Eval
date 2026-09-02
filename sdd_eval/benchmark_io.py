"""SWE-bench-compatible JSON/JSONL import and export helpers.

The default dataset export is public-safe: gold patches, test patches, and
test selectors are emitted only when ``include_oracle=True`` is explicitly
requested by an administrative caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import (
    BenchmarkInstance,
    EvaluationOracle,
    Prediction,
    RequirementIR,
    count_patch_changed_lines,
)


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


def _first_present(*values: Any) -> Any:
    return next((value for value in values if value is not None and value != ""), None)


def from_swebench_record(
    record: dict[str, Any], *, dataset_id: str, dataset_version: str = "v1", split: str = "dev"
) -> tuple[BenchmarkInstance, EvaluationOracle]:
    instance_id = str(record.get("instance_id") or "").strip()
    repo = str(record.get("repo") or "").strip()
    base_commit = str(record.get("base_commit") or "").strip()
    problem = str(record.get("problem_statement") or "").strip()
    if not instance_id or not repo or not base_commit or not problem:
        raise ValueError("instance_id, repo, base_commit, and problem_statement are required")
    oracle_data = record.get("oracle") or {}
    issue_url = _first_present(record.get("issue_url"), record.get("source_issue_url"), oracle_data.get("issue_url"))
    if not issue_url and "/" in repo and record.get("issue_id"):
        issue_url = f"https://github.com/{repo}/issues/{record['issue_id']}"
    requirements = record.get("requirements") or [
        RequirementIR(id="REQ-1", description=problem, source_refs=[str(issue_url)] if issue_url else [])
    ]
    gold_patch = str(_first_present(oracle_data.get("gold_patch"), record.get("patch"), "") or "")
    reference_code_lines = _first_present(
        record.get("reference_code_lines"),
        record.get("reference_changed_lines"),
        record.get("pr_code_lines"),
        oracle_data.get("reference_code_lines"),
        oracle_data.get("reference_changed_lines"),
    )
    if reference_code_lines is None:
        additions = _first_present(
            record.get("reference_additions"), oracle_data.get("reference_additions"),
            record.get("additions"), oracle_data.get("additions"),
        )
        deletions = _first_present(
            record.get("reference_deletions"), oracle_data.get("reference_deletions"),
            record.get("deletions"), oracle_data.get("deletions"),
        )
        if additions is not None or deletions is not None:
            try:
                reference_code_lines = int(additions or 0) + int(deletions or 0)
            except (TypeError, ValueError):
                reference_code_lines = None
    if reference_code_lines is None and gold_patch.strip():
        changed_lines = count_patch_changed_lines(gold_patch)
        if changed_lines:
            reference_code_lines = changed_lines
    reference_code_estimated = bool(
        record.get("reference_code_estimated", oracle_data.get("reference_code_estimated", False))
    )
    source_pr_url = _first_present(
        record.get("pr_url"), record.get("source_pr_url"), record.get("reference_pr_url"),
        oracle_data.get("pr_url"), oracle_data.get("source_pr_url"), oracle_data.get("reference_pr_url"),
    )
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
        environment=record.get("environment") or {},
        docker=record.get("docker") or {},
        requirements=requirements,
        constraints=record.get("constraints") or [],
        source_issue_url=str(issue_url) if issue_url else None,
        source_pr_url=str(source_pr_url) if source_pr_url else None,
        reference_code_lines=reference_code_lines,
        reference_code_estimated=reference_code_estimated,
        difficulty=str(record["difficulty"]) if record.get("difficulty") else None,
    )
    oracle = EvaluationOracle(
        instance_id=instance_id,
        oracle_version=str(oracle_data.get("oracle_version") or record.get("oracle_version") or "v1"),
        gold_patch=gold_patch,
        test_patch=str(oracle_data.get("test_patch") or record.get("test_patch") or ""),
        fail_to_pass=_test_selectors(oracle_data.get("fail_to_pass", record.get("FAIL_TO_PASS"))),
        pass_to_pass=_test_selectors(oracle_data.get("pass_to_pass", record.get("PASS_TO_PASS"))),
        forbidden_paths=oracle_data.get("forbidden_paths") or record.get("forbidden_paths") or [],
        reference_commit=str(oracle_data.get("reference_commit") or record.get("reference_commit") or "") or None,
        expected_results=oracle_data.get("expected_results") or record.get("expected_results") or {},
        quality_review=oracle_data.get("quality_review") or record.get("quality_review") or {},
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
        "reference_code_lines": instance.reference_code_lines,
        "reference_code_estimated": instance.reference_code_estimated,
        "difficulty": instance.difficulty,
        "language": instance.language,
        "schema_version": instance.schema_version,
        "evaluation_protocol": instance.evaluation_protocol,
        "dataset_id": instance.dataset_id,
        "dataset_version": instance.dataset_version,
        "split": instance.split,
        "environment_id": instance.environment_id,
        "environment": instance.environment.model_dump(mode="json"),
        "docker": instance.docker.model_dump(mode="json"),
        "requirements": [requirement.model_dump(mode="json") for requirement in instance.requirements],
        "constraints": instance.constraints,
    }
    if include_oracle:
        if oracle is None:
            raise ValueError(f"missing oracle for {instance.instance_id}")
        record.update({
            "patch": oracle.gold_patch,
            "test_patch": oracle.test_patch,
            "FAIL_TO_PASS": json.dumps(oracle.fail_to_pass),
            "PASS_TO_PASS": json.dumps(oracle.pass_to_pass),
            "oracle_version": oracle.oracle_version,
            "forbidden_paths": oracle.forbidden_paths,
            "reference_commit": oracle.reference_commit,
            "expected_results": oracle.expected_results,
            "quality_review": oracle.quality_review,
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
        json.dumps(prediction.model_dump(mode="json"), ensure_ascii=False)
        for prediction in predictions
    ]
    Path(path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
