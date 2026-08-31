from datetime import datetime, timezone
from typing import Any, Literal
import re
from urllib.parse import urlparse
from pydantic import BaseModel, Field
def now() -> datetime: return datetime.now(timezone.utc)

CLIENTS = ("codex", "opencode")
MODEL_NAMES = ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol")

def compose_client_model(client: str | None, model: str | None) -> str:
    """Return the evaluator's client:model selector while accepting legacy values."""
    selected_model = (model or "gpt-5.6-luna").strip()
    if selected_model in CLIENTS:
        return selected_model
    if selected_model.startswith(tuple(f"{client}:" for client in CLIENTS)):
        return selected_model
    selected_client = (client or "codex").strip().lower()
    if selected_client not in CLIENTS:
        raise ValueError(f"unsupported client: {selected_client}")
    aliases = {"luna": "gpt-5.6-luna", "terra": "gpt-5.6-terra", "sol": "gpt-5.6-sol"}
    selected_model = aliases.get(selected_model.lower(), selected_model)
    return f"{selected_client}:{selected_model}"

def enrich_task_metadata(task: "TaskSpec") -> "TaskSpec":
    """Fill displayable source and implementation-size metadata for a task.

    Reference PR statistics are authoritative when present. Tasks without a
    linked PR receive a clearly marked scope estimate so the catalog never
    renders an empty code-size value.
    """
    repository = (task.repository or "").strip()
    source_url = repository or (task.archive_url or "").strip()
    linked_reference = task.source_issue_url or task.reference_pr_url
    # `reference_url` is reserved for a linked issue/PR. The repository is
    # rendered separately as the project source and must not masquerade as an
    # issue reference.
    if task.source_issue_url:
        task.reference_url = task.source_issue_url
    elif not task.reference_url or (repository and task.reference_url.rstrip("/") == repository.rstrip("/")):
        task.reference_url = task.reference_pr_url or None
    provider_match = re.search(r"(?:https?://)?(?:www\.)?(github|gitee|gitcode)\.com/", repository, re.I)
    if not task.reference_provider and provider_match:
        task.reference_provider = provider_match.group(1).lower()
    if not task.reference_repo and provider_match:
        path = urlparse(repository if "://" in repository else "https://" + repository).path.strip("/")
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            task.reference_repo = "/".join(parts[:2]).removesuffix(".git")
    if task.reference_code_lines is None and task.reference_changed_lines is not None:
        task.reference_code_lines = task.reference_changed_lines
        task.reference_code_estimated = False
    if task.reference_code_lines is None and source_url:
        requirement_count = len(task.requirements)
        scenario_count = len(task.acceptance_scenarios)
        task.reference_code_lines = max(20, requirement_count * 24 + scenario_count * 12)
        task.reference_code_estimated = True
    if task.requirement_size == "unknown" and task.reference_code_lines is not None:
        lines = task.reference_code_lines
        task.requirement_size = "small" if lines <= 500 else "medium" if lines <= 1000 else "large"
    return task
class Requirement(BaseModel):
    id: str
    description: str
class AcceptanceScenario(BaseModel):
    id: str
    given: str
    when: str
    then: str
class TaskSpec(BaseModel):
    id: str
    title: str
    created_at: datetime | None = None
    repository: str | None = None
    revision: str | None = None
    archive_url: str | None = None
    language: str = "java"
    build_command: str = "./mvnw test"
    test_command: str | None = None
    requirements: list[Requirement] = Field(default_factory=list)
    acceptance_scenarios: list[AcceptanceScenario] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    source_issue_url: str | None = None
    source_issue_number: int | None = None
    reference_pr_url: str | None = None
    reference_commit: str | None = None
    reference_commit_url: str | None = None
    reference_notes: str | None = None
    reference_provider: str | None = None
    reference_repo: str | None = None
    reference_pr_number: int | None = None
    reference_changed_lines: int | None = None
    requirement_size: Literal["small", "medium", "large", "unknown"] = "unknown"
    reference_files: int | None = None
    reference_additions: int | None = None
    reference_deletions: int | None = None
    reference_url: str | None = None
    reference_code_lines: int | None = None
    reference_code_estimated: bool = False
class RunCreate(BaseModel):
    task_id: str
    task_ids: list[str] | None = None
    tool: str = "openspec"
    model: str = "gpt-5.6-luna"
    client: str | None = "codex"
    workspace: str | None = None
    models: list[str] | None = None

class ComparisonCreate(BaseModel):
    task_ids: list[str]
    models: list[str]
    tool: str = "openspec"
    client: str | None = "codex"
    workspace: str | None = None

class ComparisonResult(BaseModel):
    comparison_id: str
    task_ids: list[str]
    models: list[str]
    run_ids: list[str]
    started_at: datetime = Field(default_factory=now)

class ProjectSearchResult(BaseModel):
    provider: str
    name: str
    url: str
    description: str = ""
    default_branch: str = "main"
    stars: int = 0

class TestCollection(BaseModel):
    id: str
    name: str
    task_ids: list[str] = Field(default_factory=list)
    description: str = ""

class CollectionRunCreate(BaseModel):
    collection_id: str
    tool: str = "openspec"
    model: str = "gpt-5.6-luna"
    client: str | None = "codex"
class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True
    latency_ms: int = 0
    provider: str = "unknown"
    mode: str = "unknown"
class RunResult(BaseModel):
    run_id: str
    task_id: str
    status: Literal["queued", "running", "passed", "failed", "incomplete"]
    score: float | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    steps: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    scoring_basis: list[dict[str, Any]] = Field(default_factory=list)
    execution_mode: str = "unknown"
    generation_status: str = "unknown"
    validation: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
