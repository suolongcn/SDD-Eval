from datetime import datetime, timezone
from typing import Any, Literal
import hashlib
import re
import uuid
from urllib.parse import urlparse
from pydantic import BaseModel, Field, model_validator
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

# Benchmark V2 contracts are deliberately separate from the legacy TaskSpec
# and RunResult models. Existing tasks and historical scores remain readable,
# while new executable-oracle evaluations can evolve without reinterpreting
# legacy results.
RequirementKind = Literal[
    "functional", "boundary", "compatibility", "performance", "security",
    "concurrency", "non_functional",
]

class RequirementIR(BaseModel):
    id: str
    description: str
    kind: RequirementKind = "functional"
    priority: Literal["must", "should", "could"] = "must"
    acceptance_criteria: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    oracle_refs: list[str] = Field(default_factory=list)

class TraceLink(BaseModel):
    source_type: str
    source_id: str
    target_type: str
    target_id: str
    relation: str = "implements"
    evidence: list[str] = Field(default_factory=list)
    status: Literal["covered", "partial", "missing", "contradicted"] = "missing"
    evaluator: str = "structural"
    confidence: float | None = Field(default=None, ge=0, le=1)

class EnvironmentSpec(BaseModel):
    """Trusted local command contract; Docker will enforce it in a later phase."""
    setup_commands: list[list[str]] = Field(default_factory=list)
    build_command: list[str] | None = None
    test_command: list[str] = Field(default_factory=lambda: ["python", "-m", "pytest", "-q", "{tests}"])
    working_directory: str = "."
    setup_timeout_seconds: int = Field(default=900, ge=1)
    build_timeout_seconds: int = Field(default=600, ge=1)
    test_timeout_seconds: int = Field(default=600, ge=1)

class ContainerLimits(BaseModel):
    cpus: float = Field(default=2.0, gt=0)
    memory_mb: int = Field(default=4096, ge=128)
    pids_limit: int = Field(default=512, ge=16)
    tmpfs_mb: int = Field(default=512, ge=16)

class DockerSpec(BaseModel):
    image: str | None = None
    build_context: str | None = None
    dockerfile: str | None = None
    platform: str | None = None
    setup_network: str = "bridge"
    grading_network_disabled: bool = True
    read_only_root: bool = True
    user: str | None = None
    pull: bool = False
    limits: ContainerLimits = Field(default_factory=ContainerLimits)

class BenchmarkInstance(BaseModel):
    instance_id: str
    schema_version: int = 2
    evaluation_protocol: Literal["executable-oracle"] = "executable-oracle"
    dataset_id: str = "sdd-eval"
    dataset_version: str = "v1"
    split: Literal["train", "dev", "test", "verified", "private"] = "dev"
    repo: str
    base_commit: str
    problem_statement: str
    language: str = "python"
    environment_id: str | None = None
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    docker: DockerSpec = Field(default_factory=DockerSpec)
    version: str | None = None
    requirements: list[RequirementIR] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    source_issue_url: str | None = None
    source_pr_url: str | None = None
    difficulty: str | None = None
    created_at: datetime = Field(default_factory=now)

class EvaluationOracle(BaseModel):
    instance_id: str
    oracle_version: str = "v1"
    gold_patch: str = ""
    test_patch: str = ""
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    reference_commit: str | None = None
    expected_results: dict[str, Any] = Field(default_factory=dict)
    quality_review: dict[str, Any] = Field(default_factory=dict)

class ArtifactBundle(BaseModel):
    documents: dict[str, str] = Field(default_factory=dict)
    trace_links: list[TraceLink] = Field(default_factory=list)
    logs: dict[str, str] = Field(default_factory=dict)

class Prediction(BaseModel):
    prediction_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    instance_id: str
    run_id: str | None = None
    model_name_or_path: str
    client: str = "unknown"
    workflow: str = "direct"
    model_patch: str
    patch_hash: str = ""
    artifacts: ArtifactBundle = Field(default_factory=ArtifactBundle)
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    created_at: datetime = Field(default_factory=now)

    def model_post_init(self, __context: Any) -> None:
        calculated = hashlib.sha256(self.model_patch.encode("utf-8")).hexdigest()
        if self.patch_hash and self.patch_hash != calculated:
            raise ValueError("patch_hash does not match model_patch")
        self.patch_hash = calculated

class EvaluationResultV2(BaseModel):
    evaluation_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    prediction_id: str
    instance_id: str
    outcome: Literal[
        "resolved", "unresolved", "invalid_patch", "build_failed",
        "target_tests_failed", "regression", "agent_timeout",
        "environment_error", "harness_error",
    ]
    resolved: bool = False
    patch_applied: bool = False
    build_passed: bool = False
    fail_to_pass_total: int = Field(default=0, ge=0)
    fail_to_pass_passed: int = Field(default=0, ge=0)
    pass_to_pass_total: int = Field(default=0, ge=0)
    pass_to_pass_passed: int = Field(default=0, ge=0)
    environment_digest: str = ""
    harness_version: str = ""
    prediction_hash: str = ""
    functional_metrics: dict[str, Any] = Field(default_factory=dict)
    sdd_metrics: dict[str, Any] = Field(default_factory=dict)
    efficiency_metrics: dict[str, Any] = Field(default_factory=dict)
    execution_manifest: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.resolved != (self.outcome == "resolved"):
            raise ValueError("resolved must match the resolved outcome")
        if self.fail_to_pass_passed > self.fail_to_pass_total or self.pass_to_pass_passed > self.pass_to_pass_total:
            raise ValueError("passed test counts cannot exceed totals")
        return self

class InstanceValidationResult(BaseModel):
    instance_id: str
    valid: bool
    baseline_fail_to_pass_failed: bool = False
    baseline_pass_to_pass_passed: bool = False
    gold_patch_applied: bool = False
    gold_fail_to_pass_passed: bool = False
    gold_pass_to_pass_passed: bool = False
    errors: list[str] = Field(default_factory=list)
    logs: dict[str, str] = Field(default_factory=dict)
    environment_digest: str = ""
    harness_version: str = "local-v1"
    created_at: datetime = Field(default_factory=now)

JobStatus = Literal["queued", "preparing", "evaluating", "completed", "failed", "cancelled"]
JobKind = Literal["evaluate_prediction", "validate_instance"]

class BenchmarkJobCreate(BaseModel):
    kind: JobKind
    instance_id: str
    prediction_id: str | None = None
    backend: Literal["local", "docker"] = "docker"
    workspace: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def validate_prediction(self):
        if self.kind == "evaluate_prediction" and not self.prediction_id:
            raise ValueError("prediction_id is required for evaluation jobs")
        return self

class BenchmarkJob(BenchmarkJobCreate):
    job_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = "queued"
    attempt: int = Field(default=0, ge=0)
    worker_id: str | None = None
    result_id: str | None = None
    error: str | None = None
    cancellation_requested: bool = False
    available_at: datetime = Field(default_factory=now)
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

class JobAttempt(BaseModel):
    attempt_id: str
    job_id: str
    attempt: int
    worker_id: str
    status: Literal["running", "completed", "failed", "cancelled", "expired"] = "running"
    error: str | None = None
    result_id: str | None = None
    started_at: datetime = Field(default_factory=now)
    heartbeat_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
