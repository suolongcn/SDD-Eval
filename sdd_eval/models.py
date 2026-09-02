from datetime import datetime, timezone
from typing import Any, Literal
import hashlib
import uuid

from pydantic import BaseModel, Field, model_validator


def now() -> datetime:
    return datetime.now(timezone.utc)


def count_patch_changed_lines(patch: str) -> int:
    """Count added and removed lines in a unified diff, excluding file headers."""
    return sum(
        1
        for line in patch.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++ ", "--- "))
    )


RequirementKind = Literal[
    "functional", "boundary", "compatibility", "performance", "security",
    "concurrency", "non_functional",
]


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True
    latency_ms: int = 0
    provider: str = "unknown"
    mode: str = "unknown"


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
    reference_code_lines: int | None = Field(default=None, ge=0)
    reference_code_estimated: bool = False
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


EvaluationOutcome = Literal[
    "resolved", "unresolved", "invalid_patch", "build_failed",
    "target_tests_failed", "regression", "agent_timeout",
    "environment_error", "harness_error",
]


class EvaluationResult(BaseModel):
    evaluation_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    prediction_id: str
    instance_id: str
    outcome: EvaluationOutcome
    resolved: bool = False
    score: float = Field(default=0, ge=0, le=100)
    functional_score: float = Field(default=0, ge=0, le=100)
    code_quality_score: float = Field(default=0, ge=0, le=100)
    documentation_score: float = Field(default=0, ge=0, le=100)
    score_weights: dict[str, float] = Field(default_factory=lambda: {
        "functional": 0.50, "code_quality": 0.25, "documentation": 0.25,
    })
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
    validation_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
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


CodingClient = Literal["codex", "opencode"]
CodingModel = Literal["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
SDDWorkflow = Literal["openspec", "superpowers"]


class GenerationJobCreate(BaseModel):
    instance_id: str
    client: CodingClient = "codex"
    model: CodingModel = "gpt-5.6-sol"
    workflow: SDDWorkflow = "openspec"
    backend: Literal["local", "docker"] = "local"
    workspace: str | None = None
    max_attempts: int = Field(default=1, ge=1, le=20)


JobStatus = Literal["queued", "preparing", "generating", "evaluating", "completed", "failed", "cancelled"]
JobKind = Literal["evaluate_prediction", "validate_instance", "generate_and_evaluate"]


class BenchmarkJobCreate(BaseModel):
    kind: JobKind
    instance_id: str
    prediction_id: str | None = None
    backend: Literal["local", "docker"] = "docker"
    workspace: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=20)
    client: CodingClient | None = None
    model: CodingModel | None = None
    workflow: SDDWorkflow | None = None

    @model_validator(mode="after")
    def validate_prediction(self):
        if self.kind == "evaluate_prediction" and not self.prediction_id:
            raise ValueError("prediction_id is required for evaluation jobs")
        if self.kind == "validate_instance" and self.prediction_id:
            raise ValueError("prediction_id is not allowed for this job kind")
        if self.kind == "generate_and_evaluate" and not all((self.client, self.model, self.workflow)):
            raise ValueError("client, model, and workflow are required for generation jobs")
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
