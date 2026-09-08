"""Behavior-contract helpers used by private Cucumber evaluation oracles.

The feature files remain the immutable business oracle.  A model may supply an
adapter patch for framework-specific bindings, but it is never allowed to edit
the feature files or the runner that asserts the behavior.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def cucumber_scenarios(report: Path, required_tags: list[str]) -> list[dict[str, Any]]:
    """Read the common Cucumber JSON format and return matching scenarios.

    Cucumber implementations use either ``elements`` or ``scenarios``.  A
    scenario passes only when every executable step passed; skipped/undefined
    steps deliberately do not count as a passing business assertion.
    """
    try:
        payload = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Cucumber JSON report: {error}") from error
    features = payload if isinstance(payload, list) else payload.get("features", [])
    if not isinstance(features, list):
        raise ValueError("Cucumber JSON report must contain a feature list")
    required = {tag if tag.startswith("@") else f"@{tag}" for tag in required_tags}
    scenarios: list[dict[str, Any]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        for scenario in feature.get("elements", feature.get("scenarios", [])) or []:
            if not isinstance(scenario, dict) or scenario.get("type") in {"background", "hook"}:
                continue
            raw_tags = scenario.get("tags", []) or []
            tags = {item.get("name", "") if isinstance(item, dict) else str(item) for item in raw_tags}
            if required and not required.issubset(tags):
                continue
            steps = scenario.get("steps", []) or []
            statuses = [str((step.get("result") or {}).get("status", "unknown")).lower() for step in steps if isinstance(step, dict)]
            passed = bool(statuses) and all(status == "passed" for status in statuses)
            output = "\n".join(
                f"{step.get('keyword', '')}{step.get('name', '')}: {(step.get('result') or {}).get('status', 'unknown')}"
                for step in steps if isinstance(step, dict)
            )
            scenarios.append({
                "selector": scenario.get("name") or "unnamed scenario",
                "passed": passed,
                "returncode": 0 if passed else 1,
                "output": output,
                "tags": sorted(tags),
                "feature": feature.get("name", ""),
                "framework": "cucumber",
            })
    return scenarios


def validate_adapter_patch(patch: str, allowed_paths: list[str]) -> tuple[bool, str]:
    """Ensure a generated binding patch is limited to explicitly allowed paths."""
    if not patch.strip():
        return True, ""
    def normalized(path: str) -> str:
        path = path.replace("\\", "/")
        return path[2:] if path.startswith(("a/", "b/")) else path
    allowed = {normalized(path) for path in allowed_paths}
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            path = line[6:]
            if path != "/dev/null":
                paths.append(path)
    if not paths:
        return False, "adapter patch is not a unified diff"
    outside = sorted(set(paths) - allowed)
    return (not outside, "" if not outside else f"adapter patch modifies protected paths: {', '.join(outside)}")


def contract_digest(root: Path, paths: list[str]) -> str:
    """Hash frozen contract assets after the private test patch is applied."""
    digest = hashlib.sha256()
    for relative in sorted(paths):
        file_path = root / relative
        if not file_path.is_file():
            raise ValueError(f"frozen contract asset is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
