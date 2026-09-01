import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .models import (
    BenchmarkInstance,
    BenchmarkJob,
    ComparisonResult,
    EvaluationOracle,
    EvaluationResultV2,
    InstanceValidationResult,
    JobAttempt,
    Prediction,
    RunResult,
    TaskSpec,
    enrich_task_metadata,
    now,
)

def datetime_from_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
class Store:
    def __init__(self, path: str = "sdd_eval.db"):
        self.path = path; Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript("""
                create table if not exists tasks (id text primary key, data text not null, created_at text not null);
                create table if not exists runs (id text primary key, task_id text not null, data text not null, created_at text not null);
                create table if not exists run_artifacts (run_id text primary key, documents text not null default '{}', generated_code text not null default '', repository_code text not null default '', created_at text not null);
                create table if not exists collections (id text primary key, data text not null, created_at text not null);
                create table if not exists comparisons (id text primary key, data text not null, created_at text not null);
                create table if not exists benchmark_instances (id text primary key, dataset_id text not null, split text not null, data text not null, created_at text not null);
                create table if not exists evaluation_oracles (instance_id text primary key, data text not null, created_at text not null);
                create table if not exists predictions (id text primary key, instance_id text not null, patch_hash text not null, model_patch text not null, data text not null, created_at text not null);
                create index if not exists idx_predictions_instance on predictions(instance_id);
                create index if not exists idx_predictions_patch_hash on predictions(patch_hash);
                create table if not exists evaluation_results_v2 (id text primary key, prediction_id text not null, instance_id text not null, data text not null, created_at text not null);
                create index if not exists idx_evaluation_results_v2_prediction on evaluation_results_v2(prediction_id);
                create table if not exists instance_validations (instance_id text primary key, data text not null, created_at text not null);
                create table if not exists benchmark_jobs (id text primary key, status text not null, available_at text not null, lease_expires_at text, data text not null, created_at text not null, updated_at text not null);
                create index if not exists idx_benchmark_jobs_claim on benchmark_jobs(status, available_at, created_at);
                create table if not exists job_attempts (id text primary key, job_id text not null, attempt integer not null, status text not null, data text not null, created_at text not null);
                create index if not exists idx_job_attempts_job on job_attempts(job_id, attempt);
            """)
        self.migrate_legacy_runs()
        self.migrate_run_artifacts()
    def conn(self):
        c = sqlite3.connect(self.path, timeout=5); c.row_factory = sqlite3.Row
        c.execute("pragma busy_timeout=5000")
        return c
    def put_task(self, task):
        created_at = now()
        task.created_at = created_at
        with self.conn() as c: c.execute("insert or replace into tasks values (?, ?, ?)", (task.id, task.model_dump_json(), created_at.isoformat()))
    def get_task(self, task_id):
        with self.conn() as c: row = c.execute("select data, created_at from tasks where id=?", (task_id,)).fetchone()
        if not row: return None
        task = TaskSpec.model_validate_json(row[0])
        task.created_at = datetime_from_iso(row[1]) or task.created_at
        return enrich_task_metadata(task)
    def list_tasks(self):
        with self.conn() as c: rows = c.execute("select data, created_at from tasks order by created_at desc").fetchall()
        tasks = []
        for row in rows:
            task = TaskSpec.model_validate_json(row[0])
            task.created_at = datetime_from_iso(row[1]) or task.created_at
            tasks.append(enrich_task_metadata(task))
        return tasks

    def migrate_legacy_runs(self):
        """Invalidate runs created before real generation/validation metadata existed."""
        with self.conn() as c:
            rows = c.execute("select id, data from runs").fetchall()
            for row in rows:
                result = RunResult.model_validate_json(row[1])
                if result.execution_mode != "unknown" or result.generation_status != "unknown":
                    if result.token_usage.provider.endswith("-cli") and result.token_usage.input_tokens == 0 and result.token_usage.output_tokens == 0:
                        result.metrics["efficiency"] = 0
                        result.score = round((result.metrics.get("document", 0) * .3) + (result.metrics.get("code", 0) * .3) + (result.metrics.get("tests", 0) * .3), 2)
                        for item in result.scoring_basis:
                            if item.get("dimension") == "Efficiency":
                                item["score"] = 0
                                item["basis"] = "Codex CLI does not expose token usage to the evaluator; efficiency is not credited."
                        c.execute("update runs set data=? where id=?", (result.model_dump_json(), row[0]))
                    continue
                result.status = "incomplete"
                result.score = 0.0
                result.execution_mode = "legacy"
                result.generation_status = "unverified"
                result.validation = {"real_execution": False, "legacy": True, "reason": "Run predates real generation and validation metadata."}
                result.metrics.update({"document": 0, "code": 0, "tests": 0, "efficiency": 0, "legacy_unverified": True})
                result.scoring_basis = [{"dimension": d, "weight": w, "score": 0, "basis": "Legacy run invalidated: artifacts and model execution were not verifiable."} for d, w in (("Document quality", 30), ("Code quality", 30), ("Test quality", 30), ("Efficiency", 10))]
                c.execute("update runs set data=? where id=?", (result.model_dump_json(), row[0]))
    def delete_task(self, task_id):
        with self.conn() as c:
            c.execute("delete from runs where task_id=?", (task_id,))
            return c.execute("delete from tasks where id=?", (task_id,)).rowcount > 0
    def put_run(self, result):
        timestamp = now().isoformat()
        with self.conn() as c:
            c.execute("insert or replace into runs values (?, ?, ?, ?)", (result.run_id, result.task_id, result.model_dump_json(), timestamp))
            artifacts = result.artifacts or {}
            import json
            c.execute("insert or replace into run_artifacts(run_id, documents, generated_code, repository_code, created_at) values (?, ?, ?, ?, ?)", (result.run_id, json.dumps(artifacts.get("documents", {}), ensure_ascii=False), str(artifacts.get("generated_code", "")), str(artifacts.get("repository_code", "")), timestamp))

    def migrate_run_artifacts(self):
        """Backfill the dedicated artifact archive for runs stored before the table existed."""
        import json
        with self.conn() as c:
            rows = c.execute("select id, data, created_at from runs where id not in (select run_id from run_artifacts)").fetchall()
            for row in rows:
                result = RunResult.model_validate_json(row[1])
                artifacts = result.artifacts or {}
                if result.score is None and not artifacts.get("documents") and not artifacts.get("generated_code"):
                    message = result.error or "Run failed before artifacts were archived."
                    artifacts = {"documents": {"generation-error.md": f"# Generation failed\n\n{message}\n"}, "generated_code": f"# Generated Code\n\nGeneration failed before implementation was produced.\n\nError: {message}\n", "repository_code": artifacts.get("repository_code", "")}
                    result.artifacts = artifacts
                    result.score = 0.0
                    result.metrics.update({"document": 0, "code": 0, "tests": 0, "efficiency": 0, "artifact_archive_repaired": True})
                    result.scoring_basis = [{"dimension": d, "weight": w, "score": 0, "basis": "Failure record repaired with explicit zero score and generation-error artifact."} for d, w in (("Document quality", 30), ("Code quality", 30), ("Test quality", 30), ("Efficiency", 10))]
                    c.execute("update runs set data=? where id=?", (result.model_dump_json(), row[0]))
                c.execute("insert or ignore into run_artifacts(run_id, documents, generated_code, repository_code, created_at) values (?, ?, ?, ?, ?)", (row[0], json.dumps(artifacts.get("documents", {}), ensure_ascii=False), str(artifacts.get("generated_code", "")), str(artifacts.get("repository_code", "")), row[2]))
            # Repair failure rows that were archived by an earlier version with
            # empty artifact fields.
            rows = c.execute("select r.id, r.data, a.repository_code from runs r join run_artifacts a on a.run_id=r.id where length(a.documents) <= 2 and length(a.generated_code) = 0").fetchall()
            for row in rows:
                result = RunResult.model_validate_json(row[1])
                message = result.error or "Run failed before artifacts were archived."
                documents = {"generation-error.md": f"# Generation failed\n\n{message}\n"}
                result.artifacts = {"documents": documents, "generated_code": f"# Generated Code\n\nGeneration failed before implementation was produced.\n\nError: {message}\n", "repository_code": row[2] or ""}
                result.score = 0.0
                result.metrics.update({"document": 0, "code": 0, "tests": 0, "efficiency": 0, "artifact_archive_repaired": True})
                result.scoring_basis = [{"dimension": d, "weight": w, "score": 0, "basis": "Failure record repaired with explicit zero score and generation-error artifact."} for d, w in (("Document quality", 30), ("Code quality", 30), ("Test quality", 30), ("Efficiency", 10))]
                c.execute("update runs set data=? where id=?", (result.model_dump_json(), row[0]))
                c.execute("update run_artifacts set documents=?, generated_code=? where run_id=?", (json.dumps(documents, ensure_ascii=False), result.artifacts["generated_code"], row[0]))
    def list_runs(self):
        with self.conn() as c: rows = c.execute("select data, created_at from runs order by created_at desc").fetchall()
        results = []
        for row in rows:
            result = RunResult.model_validate_json(row[0])
            interrupted = result.artifacts.get("documents", {}).get("generation-error.md") if result.artifacts else None
            if result.status == "running" and (interrupted or (result.started_at and now() - result.started_at > timedelta(minutes=30))):
                result.status = "failed"
                result.generation_status = "failed"
                result.error = result.error or "Run interrupted before completion (worker process stopped)."
                result.finished_at = now()
                result.duration_ms = int((result.finished_at - result.started_at).total_seconds() * 1000)
                result.steps.append({"name": "Run error", "status": "failed", "duration_ms": result.duration_ms, "detail": result.error})
                with self.conn() as update:
                    update.execute("update runs set data=? where id=?", (result.model_dump_json(), result.run_id))
            if result.started_at is None:
                result.started_at = datetime_from_iso(row[1])
            if result.finished_at is None and result.duration_ms is not None:
                result.finished_at = result.started_at + timedelta(milliseconds=result.duration_ms)
            if result.duration_ms is None:
                result.duration_ms = result.metrics.get("total_duration_ms")
                if result.duration_ms is not None and result.finished_at is None:
                    result.finished_at = result.started_at + timedelta(milliseconds=result.duration_ms)
            results.append(result)
        return sorted(results, key=lambda r: r.started_at or now(), reverse=True)
    def get_archived_artifacts(self, run_id):
        import json
        with self.conn() as c:
            row = c.execute("select documents, generated_code, repository_code from run_artifacts where run_id=?", (run_id,)).fetchone()
        if not row:
            return None
        return {"documents": json.loads(row[0] or "{}"), "generated_code": row[1] or "", "repository_code": row[2] or ""}
    def delete_run(self, run_id):
        with self.conn() as c:
            return c.execute("delete from runs where id=?", (run_id,)).rowcount > 0
    def put_collection(self, collection):
        with self.conn() as c: c.execute("insert or replace into collections values (?, ?, ?)", (collection.id, collection.model_dump_json(), now().isoformat()))
    def get_collection(self, collection_id):
        with self.conn() as c: row = c.execute("select data from collections where id=?", (collection_id,)).fetchone()
        from .models import TestCollection
        return TestCollection.model_validate_json(row[0]) if row else None
    def list_collections(self):
        with self.conn() as c: rows = c.execute("select data from collections order by created_at desc").fetchall()
        from .models import TestCollection
        return [TestCollection.model_validate_json(r[0]) for r in rows]
    def delete_collection(self, collection_id):
        with self.conn() as c: return c.execute("delete from collections where id=?", (collection_id,)).rowcount > 0

    def put_comparison(self, comparison):
        with self.conn() as c:
            c.execute("insert or replace into comparisons values (?, ?, ?)", (comparison.comparison_id, comparison.model_dump_json(), comparison.started_at.isoformat()))

    def get_comparison(self, comparison_id):
        with self.conn() as c: row = c.execute("select data from comparisons where id=?", (comparison_id,)).fetchone()
        return ComparisonResult.model_validate_json(row[0]) if row else None

    def list_comparisons(self):
        with self.conn() as c: rows = c.execute("select data from comparisons order by created_at desc").fetchall()
        return [ComparisonResult.model_validate_json(row[0]) for row in rows]

    # Benchmark V2 persistence is intentionally isolated from legacy tasks and
    # runs. In particular, oracle data has no corresponding public API route.
    def put_benchmark_instance(self, instance: BenchmarkInstance):
        with self.conn() as c:
            c.execute(
                "insert or replace into benchmark_instances values (?, ?, ?, ?, ?)",
                (instance.instance_id, instance.dataset_id, instance.split, instance.model_dump_json(), instance.created_at.isoformat()),
            )

    def get_benchmark_instance(self, instance_id: str):
        with self.conn() as c:
            row = c.execute("select data from benchmark_instances where id=?", (instance_id,)).fetchone()
        return BenchmarkInstance.model_validate_json(row[0]) if row else None

    def list_benchmark_instances(self, dataset_id: str | None = None, split: str | None = None):
        query, params = "select data from benchmark_instances where 1=1", []
        if dataset_id:
            query += " and dataset_id=?"; params.append(dataset_id)
        if split:
            query += " and split=?"; params.append(split)
        query += " order by created_at desc"
        with self.conn() as c:
            rows = c.execute(query, params).fetchall()
        return [BenchmarkInstance.model_validate_json(row[0]) for row in rows]

    def put_evaluation_oracle(self, oracle: EvaluationOracle):
        with self.conn() as c:
            c.execute(
                "insert or replace into evaluation_oracles values (?, ?, ?)",
                (oracle.instance_id, oracle.model_dump_json(), now().isoformat()),
            )

    def get_evaluation_oracle(self, instance_id: str):
        with self.conn() as c:
            row = c.execute("select data from evaluation_oracles where instance_id=?", (instance_id,)).fetchone()
        return EvaluationOracle.model_validate_json(row[0]) if row else None

    def put_prediction(self, prediction: Prediction):
        with self.conn() as c:
            c.execute(
                "insert or replace into predictions values (?, ?, ?, ?, ?, ?)",
                (prediction.prediction_id, prediction.instance_id, prediction.patch_hash, prediction.model_patch, prediction.model_dump_json(), prediction.created_at.isoformat()),
            )

    def get_prediction(self, prediction_id: str):
        with self.conn() as c:
            row = c.execute("select data from predictions where id=?", (prediction_id,)).fetchone()
        return Prediction.model_validate_json(row[0]) if row else None

    def list_predictions(self, instance_id: str | None = None):
        query, params = "select data from predictions", []
        if instance_id:
            query += " where instance_id=?"; params.append(instance_id)
        query += " order by created_at desc"
        with self.conn() as c:
            rows = c.execute(query, params).fetchall()
        return [Prediction.model_validate_json(row[0]) for row in rows]

    def put_evaluation_result_v2(self, result: EvaluationResultV2):
        with self.conn() as c:
            c.execute(
                "insert or replace into evaluation_results_v2 values (?, ?, ?, ?, ?)",
                (result.evaluation_id, result.prediction_id, result.instance_id, result.model_dump_json(), result.created_at.isoformat()),
            )

    def get_evaluation_result_v2(self, evaluation_id: str):
        with self.conn() as c:
            row = c.execute("select data from evaluation_results_v2 where id=?", (evaluation_id,)).fetchone()
        return EvaluationResultV2.model_validate_json(row[0]) if row else None

    def list_evaluation_results_v2(self, instance_id: str | None = None):
        query, params = "select data from evaluation_results_v2", []
        if instance_id:
            query += " where instance_id=?"; params.append(instance_id)
        query += " order by created_at desc"
        with self.conn() as c:
            rows = c.execute(query, params).fetchall()
        return [EvaluationResultV2.model_validate_json(row[0]) for row in rows]

    def put_instance_validation(self, validation: InstanceValidationResult):
        with self.conn() as c:
            c.execute(
                "insert or replace into instance_validations values (?, ?, ?)",
                (validation.instance_id, validation.model_dump_json(), validation.created_at.isoformat()),
            )

    def get_instance_validation(self, instance_id: str):
        with self.conn() as c:
            row = c.execute("select data from instance_validations where instance_id=?", (instance_id,)).fetchone()
        return InstanceValidationResult.model_validate_json(row[0]) if row else None

    def put_job(self, job: BenchmarkJob):
        job.updated_at = now()
        with self.conn() as c:
            c.execute(
                "insert or replace into benchmark_jobs values (?, ?, ?, ?, ?, ?, ?)",
                (job.job_id, job.status, job.available_at.isoformat(), job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                 job.model_dump_json(), job.created_at.isoformat(), job.updated_at.isoformat()),
            )

    def get_job(self, job_id: str):
        with self.conn() as c: row = c.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
        return BenchmarkJob.model_validate_json(row[0]) if row else None

    def list_jobs(self, status: str | None = None):
        query, params = "select data from benchmark_jobs", []
        if status: query += " where status=?"; params.append(status)
        query += " order by created_at desc"
        with self.conn() as c: rows = c.execute(query, params).fetchall()
        return [BenchmarkJob.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _write_job(c, job: BenchmarkJob):
        job.updated_at = now()
        c.execute("update benchmark_jobs set status=?, available_at=?, lease_expires_at=?, data=?, updated_at=? where id=?",
                  (job.status, job.available_at.isoformat(), job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                   job.model_dump_json(), job.updated_at.isoformat(), job.job_id))

    def claim_job(self, worker_id: str, lease_seconds: int = 60):
        current = now()
        c = self.conn()
        try:
            c.execute("begin immediate")
            stale = c.execute("select data from benchmark_jobs where status in ('preparing','evaluating') and lease_expires_at < ?", (current.isoformat(),)).fetchall()
            for row in stale:
                job = BenchmarkJob.model_validate_json(row[0])
                terminal = job.cancellation_requested or job.attempt >= job.max_attempts
                job.status = "cancelled" if job.cancellation_requested else "failed" if terminal else "queued"
                job.error = "worker lease expired"
                job.worker_id = None; job.lease_expires_at = None; job.heartbeat_at = None
                if terminal: job.finished_at = current
                else: job.available_at = current
                self._write_job(c, job)
                attempt_id = f"{job.job_id}:{job.attempt}"
                attempt_row = c.execute("select data from job_attempts where id=?", (attempt_id,)).fetchone()
                if attempt_row:
                    attempt = JobAttempt.model_validate_json(attempt_row[0]); attempt.status = "expired"; attempt.error = job.error; attempt.finished_at = current
                    c.execute("update job_attempts set status=?, data=? where id=?", (attempt.status, attempt.model_dump_json(), attempt_id))
            row = c.execute("select data from benchmark_jobs where status='queued' and available_at <= ? and json_extract(data, '$.cancellation_requested')=0 order by created_at limit 1", (current.isoformat(),)).fetchone()
            if not row: c.commit(); return None
            job = BenchmarkJob.model_validate_json(row[0])
            job.attempt += 1; job.status = "preparing"; job.worker_id = worker_id; job.error = None
            job.started_at = job.started_at or current; job.heartbeat_at = current; job.lease_expires_at = current + timedelta(seconds=lease_seconds)
            self._write_job(c, job)
            attempt = JobAttempt(attempt_id=f"{job.job_id}:{job.attempt}", job_id=job.job_id, attempt=job.attempt, worker_id=worker_id)
            c.execute("insert into job_attempts values (?, ?, ?, ?, ?, ?)", (attempt.attempt_id, job.job_id, job.attempt, attempt.status, attempt.model_dump_json(), attempt.started_at.isoformat()))
            c.commit(); return job
        finally: c.close()

    def heartbeat_job(self, job_id: str, worker_id: str, lease_seconds: int = 60, status: str | None = None):
        with self.conn() as c:
            c.execute("begin immediate"); row = c.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return False
            job = BenchmarkJob.model_validate_json(row[0])
            if job.worker_id != worker_id or job.status not in {"preparing", "evaluating"} or job.cancellation_requested: return False
            current = now(); job.heartbeat_at = current; job.lease_expires_at = current + timedelta(seconds=lease_seconds)
            if status: job.status = status
            self._write_job(c, job)
            attempt = self._attempt_for_update(c, job)
            if attempt:
                attempt.heartbeat_at = current; c.execute("update job_attempts set data=? where id=?", (attempt.model_dump_json(), attempt.attempt_id))
            return True

    @staticmethod
    def _attempt_for_update(c, job):
        row = c.execute("select data from job_attempts where id=?", (f"{job.job_id}:{job.attempt}",)).fetchone()
        return JobAttempt.model_validate_json(row[0]) if row else None

    def finish_job(self, job_id: str, worker_id: str, result_id: str | None = None, error: str | None = None, retry_delay_seconds: int = 0):
        with self.conn() as c:
            c.execute("begin immediate"); row = c.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0])
            if job.worker_id != worker_id: return None
            current = now(); attempt = self._attempt_for_update(c, job)
            if job.cancellation_requested: job.status = "cancelled"
            elif error and job.attempt < job.max_attempts: job.status = "queued"; job.available_at = current + timedelta(seconds=retry_delay_seconds)
            elif error: job.status = "failed"
            else: job.status = "completed"; job.result_id = result_id
            job.error = error; job.worker_id = None; job.lease_expires_at = None; job.heartbeat_at = None
            if job.status in {"completed", "failed", "cancelled"}: job.finished_at = current
            self._write_job(c, job)
            if attempt:
                attempt.status = "cancelled" if job.status == "cancelled" else "failed" if error else "completed"
                attempt.error = error; attempt.result_id = result_id; attempt.finished_at = current
                c.execute("update job_attempts set status=?, data=? where id=?", (attempt.status, attempt.model_dump_json(), attempt.attempt_id))
            return job

    def request_job_cancellation(self, job_id: str):
        with self.conn() as c:
            c.execute("begin immediate"); row = c.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0]); job.cancellation_requested = True
            if job.status == "queued": job.status = "cancelled"; job.finished_at = now()
            self._write_job(c, job); return job

    def retry_job(self, job_id: str):
        with self.conn() as c:
            c.execute("begin immediate"); row = c.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0])
            if job.status not in {"failed", "cancelled"}: return None
            job.max_attempts = max(job.max_attempts, job.attempt + 1); job.status = "queued"; job.available_at = now()
            job.cancellation_requested = False; job.error = None; job.finished_at = None
            self._write_job(c, job); return job

    def list_job_attempts(self, job_id: str):
        with self.conn() as c: rows = c.execute("select data from job_attempts where job_id=? order by attempt", (job_id,)).fetchall()
        return [JobAttempt.model_validate_json(row[0]) for row in rows]
