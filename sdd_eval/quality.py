"""Deterministic quality checks for SDD documents and source patches.

The evaluator deliberately keeps these checks evidence based.  They are not a
replacement for a human design review, but they make the important review
constraints visible and scoreable: requirements must be traceable, reliability
and concurrency must be addressed (or explicitly marked not applicable), a
flow must contain success and failure paths, Java changes must satisfy the
Alibaba/P3C baseline rules, and the design must name the implementation it
claims to describe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from .models import BenchmarkInstance, EvaluationOracle, Prediction, RequirementIR, TraceLink


CODE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js",
    ".jsx", ".kt", ".kts", ".py", ".rb", ".rs", ".scala", ".swift",
    ".ts", ".tsx", ".vue", ".sql", ".xml", ".yml", ".yaml", ".properties",
}

_HA_STRATEGY = (
    "\u9ad8\u53ef\u7528", "high availability", "availability", "failover", "\u6545\u969c\u8f6c\u79fb",
    "\u5bb9\u707e", "\u707e\u5907", "disaster recovery", "multi-active", "multi active", "\u591a\u6d3b",
    "\u4e3b\u5907", "replica", "replication", "health check", "\u5065\u5eb7\u68c0\u67e5", "circuit breaker",
    "\u7194\u65ad", "degradation", "\u964d\u7ea7", "fallback", "\u964d\u7ea7",
)
_HA_TARGET = (
    "sla", "slo", "99.9", "99.99", "rto", "rpo", "\u6062\u590d\u65f6\u95f4", "\u6062\u590d\u70b9", "timeout",
    "\u8d85\u65f6", "retry", "\u91cd\u8bd5", "backoff", "\u9000\u907f", "error budget", "\u6545\u969c\u6f14\u7ec3",
)
_CONCURRENCY_STRATEGY = (
    "\u9ad8\u5e76\u53d1", "concurren", "throughput", "qps", "tps", "\u541e\u5410\u91cf", "thread", "\u7ebf\u7a0b",
    "async", "\u5f02\u6b65", "queue", "\u961f\u5217", "lock", "\u9501", "atomic", "\u539f\u5b50", "idempot",
    "\u5e42\u7b49", "partition", "\u5206\u7247", "rate limit", "\u9650\u6d41", "backpressure", "\u80cc\u538b",
)
_CONCURRENCY_TARGET = (
    "qps", "tps", "rps", "throughput", "capacity", "\u5bb9\u91cf", "benchmark", "\u57fa\u51c6", "p95", "p99",
    "latency", "\u5ef6\u8fdf", "bound", "\u4e0a\u9650", "max", "\u6700\u5927", "limit", "\u9650\u989d", "backpressure",
)
_FAILURE_TERMS = (
    "failure", "error", "exception", "timeout", "retry", "fallback", "\u5931\u8d25", "\u5f02\u5e38", "\u8d85\u65f6",
    "\u91cd\u8bd5", "\u964d\u7ea7", "\u7194\u65ad", "\u56de\u6eda", "rollback", "\u8865\u507f", "compensation",
)
_OBSERVABILITY_TERMS = (
    "metric", "metrics", "monitor", "alert", "logging", "log", "trace", "tracing", "\u6307\u6807", "\u76d1\u63a7",
    "\u544a\u8b66", "\u65e5\u5fd7", "\u94fe\u8def", "observability", "\u53ef\u89c2\u6d4b\u6027",
)
_TEST_TERMS = (
    "test", "verification", "verify", "oracle", "\u6d4b\u8bd5", "\u9a8c\u8bc1", "\u538b\u6d4b", "load test",
    "chaos", "\u6df7\u6c8c", "acceptance", "\u9a8c\u6536",
)


@dataclass(frozen=True)
class QualityFinding:
    """One evidence-bearing quality finding."""

    check_id: str
    severity: str
    message: str
    evidence: tuple[str, ...] = ()
    category: str = "quality"

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "severity": self.severity,
            "message": self.message,
            "evidence": list(self.evidence),
        }


@dataclass
class QualityReport:
    """Combined code/document report consumed by the scoring harness."""

    code_score: float
    documentation_score: float
    code_metrics: dict[str, Any] = field(default_factory=dict)
    documentation_metrics: dict[str, Any] = field(default_factory=dict)
    findings: list[QualityFinding] = field(default_factory=list)
    quality_gate: str = "pass"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code_score": self.code_score,
            "documentation_score": self.documentation_score,
            "code": self.code_metrics,
            "documentation": self.documentation_metrics,
            "findings": [finding.as_dict() for finding in self.findings],
            "quality_gate": self.quality_gate,
        }


def _lower(value: str) -> str:
    return value.casefold()


def _contains_any(text: str, terms: Iterable[str]) -> list[str]:
    lowered = _lower(text)
    return [term for term in terms if term.casefold() in lowered]


def _snippets(text: str, terms: Iterable[str], limit: int = 3) -> list[str]:
    terms = tuple(term.casefold() for term in terms)
    matches: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        lowered = line.casefold()
        if any(term in lowered for term in terms):
            matches.append(f"line {line_number}: {line.strip()[:240]}")
            if len(matches) >= limit:
                break
    return matches


def _explicit_na(text: str, labels: Iterable[str]) -> bool:
    lowered = _lower(text)
    for label in labels:
        pattern = rf"{re.escape(label.casefold())}.{{0,100}}(?:n/?a|not applicable|\u4e0d\u9002\u7528|\u65e0\u9700|\u4e0d\u652f\u6301)"
        if re.search(pattern, lowered, flags=re.IGNORECASE | re.DOTALL):
            return True
    return False


def _documents_text(documents: Mapping[str, str]) -> str:
    chunks = []
    for name, value in documents.items():
        chunks.append(f"\n--- {name} ---\n{value}")
    return "\n".join(chunks)


def changed_paths_from_patch(patch: str) -> list[str]:
    """Return normalized paths from a unified diff."""

    paths: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        match = re.match(r"diff --git a/(.+) b/(.+)$", line)
        if match:
            paths.append(match.group(2).replace("\\", "/"))
    return sorted(set(paths))


def added_lines_by_path(patch: str) -> dict[str, list[tuple[int, str]]]:
    """Extract added lines and their approximate diff line numbers."""

    result: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    line_number = 0
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            match = re.match(r"diff --git a/(.+) b/(.+)$", raw)
            current = match.group(2).replace("\\", "/") if match else None
            line_number = 0
            continue
        if current is None:
            continue
        hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
        if hunk:
            line_number = int(hunk.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            result.setdefault(current, []).append((line_number, raw[1:]))
            line_number += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw and not raw.startswith("\\"):
            line_number += 1
    return result


def _code_paths(paths: Iterable[str]) -> list[str]:
    return sorted({path for path in paths if Path(path).suffix.casefold() in CODE_EXTENSIONS})


def _path_in_text(path: str, text: str) -> bool:
    normalized = path.replace("\\", "/")
    candidates = {normalized, normalized.lstrip("./"), Path(normalized).name}
    lowered = _lower(text)
    return any(candidate.casefold() in lowered for candidate in candidates if candidate)


def _policy(oracle: EvaluationOracle | None) -> dict[str, Any]:
    raw = dict((oracle.quality_review if oracle else {}) or {})
    nested = raw.get("design_checks")
    if isinstance(nested, Mapping):
        raw.update(nested)
    return {
        "require_flowchart": bool(raw.get("require_flowchart", True)),
        "require_availability": bool(raw.get("require_availability", True)),
        "require_concurrency": bool(raw.get("require_concurrency", True)),
        "require_failure_paths": bool(raw.get("require_failure_paths", True)),
        "require_observability": bool(raw.get("require_observability", True)),
        "require_testability": bool(raw.get("require_testability", True)),
        "require_consistency": bool(raw.get("require_consistency", True)),
        "alibaba_enabled": bool(raw.get("alibaba_enabled", True)),
        "alibaba_command": raw.get("alibaba_command") or raw.get("p3c_command"),
        "style_command": raw.get("style_command") or raw.get("lint_command"),
        "coverage_command": raw.get("coverage_command"),
        "coverage_threshold": float(raw.get("coverage_threshold", 80.0)),
        "quality_timeout_seconds": int(raw.get("quality_timeout_seconds", 300)),
    }


def quality_command_policy(oracle: EvaluationOracle | None) -> dict[str, Any]:
    """Return the executable style/coverage contract for an evaluation backend."""
    policy = _policy(oracle)
    for key in ("style_command", "coverage_command"):
        if isinstance(policy[key], str):
            policy[key] = shlex.split(policy[key], posix=os.name != "nt")
        elif policy[key] is not None:
            policy[key] = [str(value) for value in policy[key]]
    return policy


def parse_coverage_percent(output: str) -> float | None:
    """Parse common coverage summaries, preferring an explicit COVERAGE marker."""
    patterns = (
        r"(?im)^\s*COVERAGE\s*[:=]\s*(\d+(?:\.\d+)?)\s*%",
        r"(?im)^\s*(?:TOTAL|Lines?|Line coverage)\b[^\n%]*?\b(\d+(?:\.\d+)?)\s*%",
        r"(?im)\bline-rate\s*=\s*[\"'](0(?:\.\d+)?|1(?:\.0+)?)",
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, output)
        if match:
            value = float(match.group(1))
            return round(value * 100 if index == 2 else value, 2)
    return None


def command_quality_metrics(
    *, style_returncode: int | None = None, style_output: str = "",
    coverage_returncode: int | None = None, coverage_output: str = "",
    coverage_threshold: float = 80.0,
) -> tuple[dict[str, Any], list[QualityFinding]]:
    """Convert configured linter and coverage command results into scored metrics."""
    findings: list[QualityFinding] = []
    if style_returncode is None:
        style = {"status": "not_configured", "score": None}
    else:
        passed = style_returncode == 0
        style = {"status": "passed" if passed else "failed", "returncode": style_returncode,
                 "score": 100.0 if passed else 0.0, "output": style_output[-8000:]}
        if not passed:
            findings.append(QualityFinding("code_style", "high", "Configured code-style check failed.", (style_output[-1000:],), "code"))
    if coverage_returncode is None:
        coverage = {"status": "not_configured", "score": None, "threshold": coverage_threshold}
    else:
        percent = parse_coverage_percent(coverage_output)
        if coverage_returncode != 0:
            status, score, message = "failed", 0.0, "Configured coverage command failed."
        elif percent is None:
            status, score, message = "unparseable", 0.0, "Coverage command did not emit a recognizable coverage percentage."
        elif percent < coverage_threshold:
            status = "below_threshold"
            score = round(percent / coverage_threshold * 100.0, 2) if coverage_threshold else 100.0
            message = f"Line coverage {percent:.2f}% is below the required {coverage_threshold:.2f}%."
        else:
            status, score, message = "passed", 100.0, ""
        coverage = {"status": status, "returncode": coverage_returncode, "percent": percent,
                    "threshold": coverage_threshold, "score": score, "output": coverage_output[-8000:]}
        if message:
            findings.append(QualityFinding("test_coverage", "high", message, (coverage_output[-1000:],), "code"))
    return {"style": style, "coverage": coverage}, findings


def check_flowchart(text: str) -> dict[str, Any]:
    """Check that a design flow has entry, branching, success, and failure paths."""

    blocks = re.findall(r"```(?:mermaid|graphviz|flowchart|dot)?\s*\n?(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    candidates = blocks or [text]
    diagram = max(candidates, key=lambda value: len(re.findall(r"(?:-->|->|=>|\u2192)", value)))
    edges = re.findall(r"(?:-->|->|=>|\u2192)", diagram)
    edge_count = len(edges)
    node_tokens: set[str] = set()
    for line in diagram.splitlines():
        if not re.search(r"-->|->|=>|\u2192", line):
            continue
        parts = re.split(r"-->|->|=>|\u2192", line, maxsplit=1)
        for part in parts:
            cleaned = re.sub(r"[\[\]{}()|;`]", " ", part).strip()
            if cleaned:
                node_tokens.add(cleaned[:100])
    start = bool(re.search(r"\b(?:start|begin|entry|request|trigger)\b|\u5f00\u59cb|\u5165\u53e3|\u8bf7\u6c42|\u89e6\u53d1", diagram, re.IGNORECASE))
    success = bool(re.search(r"\b(?:success|succeed|done|end|return|complete)\b|\u6210\u529f|\u5b8c\u6210|\u7ed3\u675f|\u8fd4\u56de", diagram, re.IGNORECASE))
    failure = bool(re.search(r"\b(?:error|fail|failure|exception|timeout|fallback|retry)\b|\u5931\u8d25|\u5f02\u5e38|\u8d85\u65f6|\u964d\u7ea7|\u91cd\u8bd5", diagram, re.IGNORECASE))
    branching = edge_count >= 2 and (len(node_tokens) >= 3 or bool(re.search(r"\b(?:if|else|decision|branch)\b|\u5224\u65ad|\u5206\u652f", diagram, re.IGNORECASE)))
    checks = {"diagram_present": bool(blocks) or edge_count > 0, "edge_count": edge_count, "node_count": len(node_tokens), "start": start, "success": success, "failure": failure, "branching": branching}
    if not checks["diagram_present"]:
        status = "missing"
    elif start and success and failure and branching:
        status = "covered"
    else:
        status = "partial"
    checks["status"] = status
    return checks


def _cross_cutting_check(
    text: str,
    check_id: str,
    strategy_terms: Sequence[str],
    target_terms: Sequence[str],
    label: str,
) -> tuple[dict[str, Any], list[QualityFinding]]:
    strategy = _contains_any(text, strategy_terms)
    target = _contains_any(text, target_terms)
    na = _explicit_na(text, strategy_terms[:4])
    if na:
        status = "not_applicable"
    elif strategy and target:
        status = "covered"
    elif strategy:
        status = "partial"
    else:
        status = "missing"
    metrics = {"status": status, "strategy_evidence": strategy[:8], "target_evidence": target[:8], "evidence": _snippets(text, (*strategy_terms, *target_terms))}
    findings: list[QualityFinding] = []
    if status == "missing":
        findings.append(QualityFinding(check_id, "high", f"Design does not address {label}; add a strategy, limits, and failure behavior or an explicit N/A rationale.", category="documentation"))
    elif status == "partial":
        findings.append(QualityFinding(check_id, "medium", f"Design mentions {label} but lacks a measurable target or bound.", tuple(metrics["evidence"]), "documentation"))
    return metrics, findings


def _traceability(
    documents: Mapping[str, str], requirements: Sequence[RequirementIR], links: Sequence[TraceLink], changed_paths: Sequence[str],
) -> tuple[dict[str, Any], list[QualityFinding]]:
    text = _documents_text(documents)
    design_text = "\n".join(value for name, value in documents.items() if "design" in name.casefold() or "plan" in name.casefold())
    rows: list[dict[str, Any]] = []
    findings: list[QualityFinding] = []
    code_paths = set(_code_paths(changed_paths))
    for requirement in requirements:
        req_links = [link for link in links if link.source_type.casefold() in {"requirement", "req"} and link.source_id == requirement.id]
        doc_present = requirement.id.casefold() in text.casefold() or requirement.description.casefold() in text.casefold()
        design_present = requirement.id.casefold() in design_text.casefold() or requirement.description.casefold() in design_text.casefold()
        covered_link = any(link.status == "covered" for link in req_links)
        contradictory = any(link.status == "contradicted" for link in req_links)
        code_link = [link for link in req_links if link.target_type.casefold() == "code"]
        test_link = [link for link in req_links if link.target_type.casefold() in {"test", "tests"}]
        code_evidence = any(_path_in_text(link.target_id, "\n".join(changed_paths)) for link in code_link) or any(link.target_id in text for link in code_link)
        test_evidence = bool(test_link) or bool(requirement.acceptance_criteria and _contains_any(text, _TEST_TERMS))
        if contradictory:
            status = "contradicted"
        elif doc_present and design_present and covered_link and code_evidence and test_evidence:
            status = "covered"
        elif doc_present or design_present or req_links:
            status = "partial"
        else:
            status = "missing"
        row = {"id": requirement.id, "status": status, "document": doc_present, "design": design_present, "code": code_evidence, "test": test_evidence, "links": len(req_links)}
        rows.append(row)
        if status in {"missing", "contradicted"}:
            severity = "blocker" if requirement.priority == "must" else "high"
            findings.append(QualityFinding("requirements_traceability", severity, f"Requirement {requirement.id} is {status}; link its design, implementation, and verification evidence.", category="documentation"))
        elif status == "partial":
            findings.append(QualityFinding("requirements_traceability", "medium", f"Requirement {requirement.id} has incomplete design/code/test evidence.", category="documentation"))
    if not requirements:
        metrics = {"status": "not_applicable", "total": 0, "covered": 0, "partial": 0, "missing": 0, "rows": []}
    else:
        counts = {status: sum(row["status"] == status for row in rows) for status in ("covered", "partial", "missing", "contradicted")}
        metrics = {"status": "covered" if counts["covered"] == len(rows) else "partial" if counts["covered"] or counts["partial"] else "missing", "total": len(rows), **counts, "rows": rows}
    return metrics, findings


def _consistency(
    documents: Mapping[str, str], links: Sequence[TraceLink], changed_paths: Sequence[str],
) -> tuple[dict[str, Any], list[QualityFinding]]:
    text = _documents_text(documents)
    code_paths = _code_paths(changed_paths)
    referenced = [path for path in code_paths if _path_in_text(path, text) or any(_path_in_text(path, link.target_id) for link in links if link.target_type.casefold() == "code")]
    stale: list[str] = []
    for link in links:
        if link.target_type.casefold() != "code":
            continue
        target = link.target_id.replace("\\", "/")
        if Path(target).suffix.casefold() in CODE_EXTENSIONS and not any(_path_in_text(target, path) for path in code_paths):
            stale.append(target)
    if not code_paths:
        status = "not_applicable"
    elif stale or len(referenced) < len(code_paths):
        status = "partial" if referenced else "missing"
    else:
        status = "covered"
    metrics = {"status": status, "changed_code_paths": code_paths, "referenced_code_paths": referenced, "unreferenced_code_paths": [path for path in code_paths if path not in referenced], "stale_code_references": sorted(set(stale))}
    findings: list[QualityFinding] = []
    if stale:
        findings.append(QualityFinding("implementation_consistency", "high", "Trace links reference code paths that are not part of the implementation patch.", tuple(stale), "consistency"))
    if status in {"missing", "partial"} and code_paths:
        findings.append(QualityFinding("implementation_consistency", "high" if status == "missing" else "medium", "Design documentation does not consistently identify the changed production code.", tuple(metrics["unreferenced_code_paths"]), "consistency"))
    return metrics, findings


_ALIBABA_RULES: tuple[tuple[str, str, str, int, str], ...] = (
    ("ALI-IMPORT-WILDCARD", r"^\s*import\s+[\w.]+\.\*\s*;", "Wildcard imports are forbidden by Alibaba Java guidelines.", 12, "high"),
    ("ALI-SYSTEM-OUT", r"\bSystem\.(?:out|err)\.(?:print|println|printf)\s*\(", "Use a structured logger instead of System.out/System.err.", 10, "high"),
    ("ALI-STACKTRACE", r"\b(?:printStackTrace|e\.printStackTrace)\s*\(", "Do not print stack traces directly; log with context.", 10, "high"),
    ("ALI-THREAD-SLEEP", r"\bThread\.sleep\s*\(", "Thread.sleep in service code can block request threads; use a bounded scheduler or async mechanism.", 5, "medium"),
    ("ALI-BIGDECIMAL-DOUBLE", r"new\s+BigDecimal\s*\(\s*[^\"\d][^)]*\)", "Construct BigDecimal from a String or valueOf to avoid floating-point precision loss.", 8, "medium"),
    ("ALI-STRING-EQUALITY", r"(?:\"[^\"]*\"|\bString\s+\w+)\s*==\s*(?:\"[^\"]*\"|\w+)", "Compare Java strings with equals/Objects.equals, not ==.", 8, "high"),
    ("ALI-EMPTY-CATCH", r"catch\s*\([^)]*\)\s*\{\s*\}", "Empty catch blocks hide failures and violate exception-handling guidance.", 10, "high"),
)


def check_alibaba_java(
    patch: str,
    *,
    root: Path | None = None,
    policy: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], list[QualityFinding]]:
    """Run P3C when configured and always apply deterministic Java guardrails."""

    policy = policy or {}
    if policy.get("alibaba_enabled") is False:
        return {"applicable": False, "status": "disabled", "standard": "Alibaba Java Coding Guidelines (P3C)", "tool": "disabled", "violations": []}, []
    java_lines = {path: lines for path, lines in added_lines_by_path(patch).items() if path.casefold().endswith(".java")}
    if not java_lines:
        return {"applicable": False, "status": "not_applicable", "standard": "Alibaba Java Coding Guidelines (P3C)", "tool": "not_applicable", "violations": []}, []
    findings: list[QualityFinding] = []
    violations: list[dict[str, Any]] = []
    for path, lines in java_lines.items():
        for line_number, line in lines:
            if len(line) > 120:
                violations.append({"rule": "ALI-LINE-LENGTH", "path": path, "line": line_number, "message": "Keep Java lines within the project line-length limit.", "severity": "medium"})
            for rule_id, pattern, message, _penalty, severity in _ALIBABA_RULES:
                if re.search(pattern, line):
                    violations.append({"rule": rule_id, "path": path, "line": line_number, "message": message, "severity": severity})
    command = policy.get("alibaba_command") or os.environ.get("SDD_EVAL_ALIBABA_COMMAND")
    tool = "p3c-static-compatible"
    tool_status = "fallback"
    tool_output = ""
    if command and root:
        try:
            argv = shlex.split(command) if isinstance(command, str) else list(command)
            process = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            tool = "configured"
            tool_status = "passed" if process.returncode == 0 else "failed"
            tool_output = ((process.stdout or "") + (process.stderr or ""))[-8000:]
            if process.returncode:
                violations.append({"rule": "P3C-COMMAND", "message": "Configured Alibaba/P3C checker reported violations.", "severity": "high", "output": tool_output})
        except (OSError, subprocess.TimeoutExpired) as error:
            tool_status = "error"
            tool_output = str(error)
            findings.append(QualityFinding("alibaba_java", "medium", "Configured Alibaba/P3C checker could not be executed; static fallback was used.", (tool_output,), "code"))
    for violation in violations:
        evidence = [f"{violation.get('path', 'checker')}:{violation.get('line', '-')}"]
        findings.append(QualityFinding("alibaba_java", violation["severity"], violation["message"], tuple(evidence), "code"))
    penalty = sum(12 if item["severity"] == "high" else 6 for item in violations)
    score = max(0.0, round(100.0 - min(100, penalty), 2))
    metrics = {"applicable": True, "status": "covered" if not violations else "failed", "standard": "Alibaba Java Coding Guidelines (P3C)", "tool": tool, "tool_status": tool_status, "tool_output": tool_output, "violations": violations, "score": score}
    return metrics, findings


def _legacy_document_score(documents: Mapping[str, str], links: Sequence[TraceLink]) -> float:
    values = {str(name): str(value).strip() for name, value in documents.items()}
    non_empty = sum(bool(value) for value in values.values())
    named_docs = sum(any(token in name.casefold() for token in ("spec", "design", "requirement", "plan")) for name in values)
    covered_links = sum(link.status == "covered" for link in links)
    if not values:
        return 0.0
    return min(100.0, round((min(non_empty, 2) / 2 * 60.0) + (min(named_docs, 2) / 2 * 20.0) + (covered_links > 0) * 20.0, 2))


def assess_quality(
    prediction: Prediction,
    instance: BenchmarkInstance,
    oracle: EvaluationOracle | None = None,
    *,
    patch_applied: bool = True,
    build_passed: bool = True,
    error_kind: str | None = None,
    forbidden_changes: Sequence[str] = (),
    functional_score: float | None = None,
    precomputed_code_quality: Mapping[str, Any] | None = None,
    precomputed_quality_findings: Sequence[Mapping[str, Any]] = (),
) -> QualityReport:
    """Assess all quality dimensions without changing functional outcomes."""

    documents = {str(name): str(value) for name, value in prediction.artifacts.documents.items()}
    links = prediction.artifacts.trace_links
    paths = changed_paths_from_patch(prediction.model_patch)
    policy = _policy(oracle)
    if precomputed_code_quality is not None:
        code_metrics = dict(precomputed_code_quality)
        findings = [
            QualityFinding(
                str(item.get("check_id", "alibaba_java")),
                str(item.get("severity", "medium")),
                str(item.get("message", "Alibaba/P3C check reported a finding.")),
                tuple(str(value) for value in item.get("evidence", ()) or ()),
                str(item.get("category", "code")),
            )
            for item in precomputed_quality_findings
        ]
    else:
        code_metrics, findings = check_alibaba_java(prediction.model_patch, policy=policy)
    additions = [line[1:] for line in prediction.model_patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    hygiene_penalty = sum(line.rstrip() != line for line in additions)
    if error_kind or not patch_applied or forbidden_changes or not build_passed:
        code_score = 0.0
    else:
        code_score = max(0.0, round(100.0 - min(40.0, hygiene_penalty * 5.0), 2))
    if code_metrics.get("applicable"):
        code_score = round(code_score * float(code_metrics.get("score", 0)) / 100.0, 2)
    component_scores = [code_score]
    for key in ("style", "coverage"):
        value = code_metrics.get("command_checks", {}).get(key, {}).get("score")
        if value is not None:
            component_scores.append(float(value))
    code_score = round(sum(component_scores) / len(component_scores), 2)
    code_metrics.update({"patch_hygiene_penalty": hygiene_penalty, "changed_paths": paths, "score": code_score})

    # Keep scores for old, hand-written fixtures stable.  As soon as an
    # instance has requirements or an explicit policy, the strict design
    # contract below is used.
    strict_docs = bool(instance.requirements or (oracle and oracle.quality_review))
    if not strict_docs:
        if not documents and not links and functional_score is not None:
            # Predictions created before artifact capture used the executable
            # score for both quality dimensions. Preserve that contract while
            # still applying the new checks to strict reviews.
            code_score = functional_score
            documentation_score = functional_score
        else:
            documentation_score = _legacy_document_score(documents, links)
        documentation_metrics = {"status": "legacy", "strict": False, "score": documentation_score, "document_count": len(documents), "trace_link_count": len(links)}
    else:
        trace_metrics, trace_findings = _traceability(documents, instance.requirements, links, paths)
        findings.extend(trace_findings)
        documentation_metrics: dict[str, Any] = {
            "strict": True,
            "document_count": len(documents),
            "documents": sorted(documents),
            "trace_link_count": len(links),
            "covered_trace_links": sum(link.status == "covered" for link in links),
            "traceability": trace_metrics,
        }
        weighted: list[tuple[str, float, dict[str, Any], bool]] = [("traceability", 20.0, trace_metrics, bool(instance.requirements))]
        for key, terms, target, label, weight, required in (
            ("availability", _HA_STRATEGY, _HA_TARGET, "high availability", 15.0, policy["require_availability"]),
            ("concurrency", _CONCURRENCY_STRATEGY, _CONCURRENCY_TARGET, "high concurrency", 15.0, policy["require_concurrency"]),
        ):
            metrics, check_findings = _cross_cutting_check(_documents_text(documents), key, terms, target, label)
            documentation_metrics[key] = metrics
            findings.extend(check_findings if required else [])
            weighted.append((key, weight, metrics, required))
        flow = check_flowchart(_documents_text(documents))
        documentation_metrics["flowchart"] = flow
        if policy["require_flowchart"] and flow["status"] != "covered":
            findings.append(QualityFinding("flowchart_completeness", "high" if flow["status"] == "missing" else "medium", "Design flowchart must show entry, decisions, success, and failure paths.", category="documentation"))
        weighted.append(("flowchart", 15.0, flow, policy["require_flowchart"]))
        for key, terms, label, weight, required in (("failure_paths", _FAILURE_TERMS, "failure handling", 10.0, policy["require_failure_paths"]), ("observability", _OBSERVABILITY_TERMS, "observability", 10.0, policy["require_observability"]), ("testability", _TEST_TERMS, "verification", 10.0, policy["require_testability"])):
            evidence = _contains_any(_documents_text(documents), terms)
            na = _explicit_na(_documents_text(documents), terms[:3])
            status = "not_applicable" if na else "covered" if evidence else "missing"
            metrics = {"status": status, "evidence": evidence[:8], "snippets": _snippets(_documents_text(documents), terms)}
            documentation_metrics[key] = metrics
            if required and status == "missing":
                findings.append(QualityFinding(key, "high", f"Design does not specify {label}.", category="documentation"))
            weighted.append((key, weight, metrics, required))
        consistency, consistency_findings = _consistency(documents, links, paths)
        documentation_metrics["implementation_consistency"] = consistency
        findings.extend(consistency_findings if policy["require_consistency"] else [])
        weighted.append(("implementation_consistency", 5.0, consistency, policy["require_consistency"]))
        applicable = [(weight, metrics) for _key, weight, metrics, required in weighted if required and metrics.get("status") != "not_applicable"]
        points = 0.0
        denominator = sum(weight for weight, _metrics in applicable) or 1.0
        for weight, metrics in applicable:
            points += weight * {"covered": 1.0, "partial": 0.5, "missing": 0.0, "contradicted": 0.0}.get(metrics.get("status"), 0.0)
        documentation_score = round(points / denominator * 100.0, 2)
        documentation_metrics["score"] = documentation_score

    if error_kind or any(finding.severity == "blocker" for finding in findings):
        gate = "fail" if error_kind or findings else "pass"
    elif any(finding.severity in {"high", "medium"} for finding in findings):
        gate = "conditional"
    else:
        gate = "pass"
    return QualityReport(code_score, documentation_score, code_metrics, documentation_metrics, findings, gate)
