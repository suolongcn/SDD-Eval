import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import (
    BenchmarkInstance, BenchmarkJob, EvaluationOracle, EvaluationResult,
    InstanceValidationResult, JobAttempt, Prediction, count_patch_changed_lines, now,
)


SCHEMA_VERSION = 3
V2_TABLES = {
    "benchmark_instances", "evaluation_oracles", "predictions", "evaluation_results",
    "instance_validations", "benchmark_jobs", "job_attempts", "schema_metadata",
}


class Store:
    """V2-only SQLite store. Any pre-V2 application schema is intentionally discarded."""

    def __init__(self, path: str = "sdd_eval.db"):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def conn(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma busy_timeout=5000")
        connection.execute("pragma foreign_keys=on")
        return connection

    def _initialize(self):
        with self.conn() as connection:
            existing = {row[0] for row in connection.execute("select name from sqlite_master where type='table'")}
            version_row = None
            if "schema_metadata" in existing:
                version_row = connection.execute("select value from schema_metadata where key='schema_version'").fetchone()
            try:
                stored_version = int(version_row[0]) if version_row else None
            except (TypeError, ValueError):
                stored_version = None
            if existing - {"sqlite_sequence"} and stored_version != SCHEMA_VERSION:
                for table in existing - {"sqlite_sequence"}:
                    quoted_table = table.replace('"', '""')
                    connection.execute(f'drop table if exists "{quoted_table}"')
            connection.executescript("""
                create table if not exists schema_metadata (key text primary key, value text not null);
                create table if not exists benchmark_instances (id text primary key, dataset_id text not null, split text not null, data text not null, created_at text not null);
                create table if not exists evaluation_oracles (instance_id text primary key references benchmark_instances(id) on delete cascade, data text not null, created_at text not null);
                create table if not exists predictions (id text primary key, instance_id text not null references benchmark_instances(id) on delete cascade, patch_hash text not null, data text not null, created_at text not null);
                create index if not exists idx_predictions_instance on predictions(instance_id);
                create index if not exists idx_predictions_patch_hash on predictions(patch_hash);
                create table if not exists evaluation_results (id text primary key, prediction_id text not null references predictions(id) on delete cascade, instance_id text not null references benchmark_instances(id) on delete cascade, outcome text not null, data text not null, created_at text not null);
                create index if not exists idx_results_prediction on evaluation_results(prediction_id);
                create index if not exists idx_results_instance on evaluation_results(instance_id);
                create table if not exists instance_validations (id text primary key, instance_id text not null references benchmark_instances(id) on delete cascade, valid integer not null, data text not null, created_at text not null);
                create index if not exists idx_validations_instance on instance_validations(instance_id, created_at);
                create table if not exists benchmark_jobs (id text primary key, instance_id text not null references benchmark_instances(id) on delete cascade, prediction_id text references predictions(id) on delete cascade, status text not null, available_at text not null, lease_expires_at text, data text not null, created_at text not null, updated_at text not null);
                create index if not exists idx_jobs_claim on benchmark_jobs(status, available_at, created_at);
                create table if not exists job_attempts (id text primary key, job_id text not null references benchmark_jobs(id) on delete cascade, attempt integer not null, status text not null, data text not null, created_at text not null);
                create index if not exists idx_attempts_job on job_attempts(job_id, attempt);
            """)
            connection.execute("insert or replace into schema_metadata values ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._backfill_reference_code_lines(connection)

    @staticmethod
    def _backfill_reference_code_lines(connection):
        """Persist public PR line counts derived from stored private patches."""
        rows = connection.execute(
            "select benchmark_instances.id, benchmark_instances.data, evaluation_oracles.data "
            "from benchmark_instances join evaluation_oracles "
            "on evaluation_oracles.instance_id = benchmark_instances.id"
        ).fetchall()
        for instance_id, instance_data, oracle_data in rows:
            instance = BenchmarkInstance.model_validate_json(instance_data)
            if instance.reference_code_lines is not None and not instance.reference_code_estimated:
                continue
            oracle = EvaluationOracle.model_validate_json(oracle_data)
            if not oracle.gold_patch.strip():
                continue
            changed_lines = count_patch_changed_lines(oracle.gold_patch)
            if not changed_lines:
                continue
            instance.reference_code_lines = changed_lines
            instance.reference_code_estimated = False
            connection.execute(
                "update benchmark_instances set data=? where id=?",
                (instance.model_dump_json(), instance_id),
            )

    def put_benchmark_instance(self, instance: BenchmarkInstance, oracle: EvaluationOracle | None = None):
        if oracle and (instance.reference_code_lines is None or instance.reference_code_estimated) and oracle.gold_patch.strip():
            changed_lines = count_patch_changed_lines(oracle.gold_patch)
            if changed_lines:
                instance.reference_code_lines = changed_lines
                instance.reference_code_estimated = False
        with self.conn() as connection:
            connection.execute(
                "insert into benchmark_instances values (?, ?, ?, ?, ?) on conflict(id) do update set dataset_id=excluded.dataset_id, split=excluded.split, data=excluded.data, created_at=excluded.created_at",
                (instance.instance_id, instance.dataset_id, instance.split, instance.model_dump_json(), instance.created_at.isoformat()),
            )
            if oracle:
                self._put_oracle(connection, oracle)

    def get_benchmark_instance(self, instance_id: str):
        with self.conn() as connection:
            row = connection.execute("select data from benchmark_instances where id=?", (instance_id,)).fetchone()
        return BenchmarkInstance.model_validate_json(row[0]) if row else None

    def list_benchmark_instances(self, dataset_id: str | None = None, split: str | None = None):
        clauses, params = [], []
        if dataset_id: clauses.append("dataset_id=?"); params.append(dataset_id)
        if split: clauses.append("split=?"); params.append(split)
        query = "select data from benchmark_instances"
        if clauses: query += " where " + " and ".join(clauses)
        query += " order by created_at desc"
        with self.conn() as connection: rows = connection.execute(query, params).fetchall()
        return [BenchmarkInstance.model_validate_json(row[0]) for row in rows]

    def delete_benchmark_instance(self, instance_id: str):
        with self.conn() as connection:
            return connection.execute("delete from benchmark_instances where id=?", (instance_id,)).rowcount > 0

    @staticmethod
    def _put_oracle(connection, oracle: EvaluationOracle):
        connection.execute("insert into evaluation_oracles values (?, ?, ?) on conflict(instance_id) do update set data=excluded.data, created_at=excluded.created_at",
                           (oracle.instance_id, oracle.model_dump_json(), now().isoformat()))

    def put_evaluation_oracle(self, oracle: EvaluationOracle):
        with self.conn() as connection:
            self._put_oracle(connection, oracle)
            self._backfill_reference_code_lines(connection)

    def get_evaluation_oracle(self, instance_id: str):
        with self.conn() as connection:
            row = connection.execute("select data from evaluation_oracles where instance_id=?", (instance_id,)).fetchone()
        return EvaluationOracle.model_validate_json(row[0]) if row else None

    def put_prediction(self, prediction: Prediction):
        with self.conn() as connection:
            connection.execute("insert into predictions values (?, ?, ?, ?, ?) on conflict(id) do update set instance_id=excluded.instance_id, patch_hash=excluded.patch_hash, data=excluded.data, created_at=excluded.created_at",
                               (prediction.prediction_id, prediction.instance_id, prediction.patch_hash,
                                prediction.model_dump_json(), prediction.created_at.isoformat()))

    def get_prediction(self, prediction_id: str):
        with self.conn() as connection:
            row = connection.execute("select data from predictions where id=?", (prediction_id,)).fetchone()
        return Prediction.model_validate_json(row[0]) if row else None

    def list_predictions(self, instance_id: str | None = None):
        query, params = "select data from predictions", []
        if instance_id: query += " where instance_id=?"; params.append(instance_id)
        query += " order by created_at desc"
        with self.conn() as connection: rows = connection.execute(query, params).fetchall()
        return [Prediction.model_validate_json(row[0]) for row in rows]

    def put_evaluation_result(self, result: EvaluationResult):
        with self.conn() as connection:
            connection.execute("insert or replace into evaluation_results values (?, ?, ?, ?, ?, ?)",
                               (result.evaluation_id, result.prediction_id, result.instance_id, result.outcome,
                                result.model_dump_json(), result.created_at.isoformat()))

    @staticmethod
    def _evaluation_result(data: str) -> EvaluationResult:
        payload = json.loads(data)
        score_was_missing = "score" not in payload
        if score_was_missing:
            fail_total = payload.get("fail_to_pass_total", 0)
            pass_total = payload.get("pass_to_pass_total", 0)
            fail_rate = payload.get("fail_to_pass_passed", 0) / fail_total if fail_total else 0
            pass_rate = payload.get("pass_to_pass_passed", 0) / pass_total if pass_total else 1
            unscored = payload.get("outcome") in {"invalid_patch", "build_failed", "agent_timeout", "environment_error", "harness_error"}
            payload["score"] = 0 if unscored else round(((fail_rate + pass_rate) / 2) * 100, 2)
        # Backfill score dimensions for results written before composite scoring.
        functional = payload.get("score", 0) if score_was_missing else payload.get("functional_score", payload.get("score", 0))
        if score_was_missing:
            payload["functional_score"] = functional
            payload["code_quality_score"] = functional
            payload["documentation_score"] = functional
        else:
            payload.setdefault("functional_score", functional)
            payload.setdefault("code_quality_score", functional)
            payload.setdefault("documentation_score", functional)
        payload.setdefault("score_weights", {"functional": 0.50, "code_quality": 0.25, "documentation": 0.25})
        return EvaluationResult.model_validate(payload)

    def get_evaluation_result(self, evaluation_id: str):
        with self.conn() as connection:
            row = connection.execute("select data from evaluation_results where id=?", (evaluation_id,)).fetchone()
        return self._evaluation_result(row[0]) if row else None

    def list_evaluation_results(self, instance_id: str | None = None, prediction_id: str | None = None):
        clauses, params = [], []
        if instance_id: clauses.append("instance_id=?"); params.append(instance_id)
        if prediction_id: clauses.append("prediction_id=?"); params.append(prediction_id)
        query = "select data from evaluation_results"
        if clauses: query += " where " + " and ".join(clauses)
        query += " order by created_at desc"
        with self.conn() as connection: rows = connection.execute(query, params).fetchall()
        return [self._evaluation_result(row[0]) for row in rows]

    def put_instance_validation(self, validation: InstanceValidationResult):
        with self.conn() as connection:
            connection.execute("insert or replace into instance_validations values (?, ?, ?, ?, ?)",
                               (validation.validation_id, validation.instance_id, int(validation.valid),
                                validation.model_dump_json(), validation.created_at.isoformat()))

    def get_instance_validation(self, instance_id: str):
        with self.conn() as connection:
            row = connection.execute("select data from instance_validations where instance_id=? order by created_at desc limit 1", (instance_id,)).fetchone()
        return InstanceValidationResult.model_validate_json(row[0]) if row else None

    def list_instance_validations(self, instance_id: str | None = None):
        query, params = "select data from instance_validations", []
        if instance_id: query += " where instance_id=?"; params.append(instance_id)
        query += " order by created_at desc"
        with self.conn() as connection: rows = connection.execute(query, params).fetchall()
        return [InstanceValidationResult.model_validate_json(row[0]) for row in rows]

    def put_job(self, job: BenchmarkJob):
        job.updated_at = now()
        with self.conn() as connection:
            connection.execute("insert into benchmark_jobs values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict(id) do update set instance_id=excluded.instance_id, prediction_id=excluded.prediction_id, status=excluded.status, available_at=excluded.available_at, lease_expires_at=excluded.lease_expires_at, data=excluded.data, updated_at=excluded.updated_at",
                               (job.job_id, job.instance_id, job.prediction_id, job.status, job.available_at.isoformat(),
                                job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                                job.model_dump_json(), job.created_at.isoformat(), job.updated_at.isoformat()))

    def get_job(self, job_id: str):
        with self.conn() as connection:
            row = connection.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
        return BenchmarkJob.model_validate_json(row[0]) if row else None

    def list_jobs(self, status: str | None = None, instance_id: str | None = None):
        clauses, params = [], []
        if status: clauses.append("status=?"); params.append(status)
        if instance_id: clauses.append("instance_id=?"); params.append(instance_id)
        query = "select data from benchmark_jobs"
        if clauses: query += " where " + " and ".join(clauses)
        query += " order by created_at desc"
        with self.conn() as connection: rows = connection.execute(query, params).fetchall()
        return [BenchmarkJob.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _write_job(connection, job: BenchmarkJob):
        job.updated_at = now()
        connection.execute("update benchmark_jobs set prediction_id=?, status=?, available_at=?, lease_expires_at=?, data=?, updated_at=? where id=?",
                           (job.prediction_id, job.status, job.available_at.isoformat(), job.lease_expires_at.isoformat() if job.lease_expires_at else None,
                            job.model_dump_json(), job.updated_at.isoformat(), job.job_id))

    @staticmethod
    def _attempt_for_update(connection, job: BenchmarkJob):
        row = connection.execute("select data from job_attempts where id=?", (f"{job.job_id}:{job.attempt}",)).fetchone()
        return JobAttempt.model_validate_json(row[0]) if row else None

    def claim_job(self, worker_id: str, lease_seconds: int = 60):
        current = now()
        connection = self.conn()
        try:
            connection.execute("begin immediate")
            stale = connection.execute("select data from benchmark_jobs where status in ('preparing','generating','evaluating') and lease_expires_at < ?", (current.isoformat(),)).fetchall()
            for row in stale:
                job = BenchmarkJob.model_validate_json(row[0])
                terminal = job.cancellation_requested or job.attempt >= job.max_attempts
                job.status = "cancelled" if job.cancellation_requested else "failed" if terminal else "queued"
                job.error = "worker lease expired"
                job.worker_id = None; job.lease_expires_at = None; job.heartbeat_at = None
                if terminal: job.finished_at = current
                else: job.available_at = current
                self._write_job(connection, job)
                attempt = self._attempt_for_update(connection, job)
                if attempt:
                    attempt.status = "expired"; attempt.error = job.error; attempt.finished_at = current
                    connection.execute("update job_attempts set status=?, data=? where id=?", (attempt.status, attempt.model_dump_json(), attempt.attempt_id))
            row = connection.execute("select data from benchmark_jobs where status='queued' and available_at <= ? and json_extract(data, '$.cancellation_requested')=0 order by created_at limit 1", (current.isoformat(),)).fetchone()
            if not row: connection.commit(); return None
            job = BenchmarkJob.model_validate_json(row[0])
            job.attempt += 1; job.status = "preparing"; job.worker_id = worker_id; job.error = None
            job.started_at = job.started_at or current; job.heartbeat_at = current; job.lease_expires_at = current + timedelta(seconds=lease_seconds)
            self._write_job(connection, job)
            attempt = JobAttempt(attempt_id=f"{job.job_id}:{job.attempt}", job_id=job.job_id, attempt=job.attempt, worker_id=worker_id)
            connection.execute("insert into job_attempts values (?, ?, ?, ?, ?, ?)",
                               (attempt.attempt_id, job.job_id, job.attempt, attempt.status, attempt.model_dump_json(), attempt.started_at.isoformat()))
            connection.commit(); return job
        finally:
            connection.close()

    def heartbeat_job(self, job_id: str, worker_id: str, lease_seconds: int = 60, status: str | None = None):
        with self.conn() as connection:
            connection.execute("begin immediate")
            row = connection.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return False
            job = BenchmarkJob.model_validate_json(row[0])
            if job.worker_id != worker_id or job.status not in {"preparing", "generating", "evaluating"} or job.cancellation_requested: return False
            current = now(); job.heartbeat_at = current; job.lease_expires_at = current + timedelta(seconds=lease_seconds)
            if status: job.status = status
            self._write_job(connection, job)
            attempt = self._attempt_for_update(connection, job)
            if attempt:
                attempt.heartbeat_at = current
                connection.execute("update job_attempts set data=? where id=?", (attempt.model_dump_json(), attempt.attempt_id))
            return True

    def attach_job_prediction(self, job_id: str, worker_id: str, prediction_id: str):
        with self.conn() as connection:
            connection.execute("begin immediate")
            row = connection.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0])
            if job.worker_id != worker_id or job.cancellation_requested: return None
            job.prediction_id = prediction_id
            job.updated_at = now()
            connection.execute(
                "update benchmark_jobs set prediction_id=?, data=?, updated_at=? where id=?",
                (prediction_id, job.model_dump_json(), job.updated_at.isoformat(), job_id),
            )
            return job

    def finish_job(self, job_id: str, worker_id: str, result_id: str | None = None, error: str | None = None, retry_delay_seconds: int = 0):
        with self.conn() as connection:
            connection.execute("begin immediate")
            row = connection.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0])
            if job.worker_id != worker_id: return None
            current = now(); attempt = self._attempt_for_update(connection, job)
            if job.cancellation_requested: job.status = "cancelled"
            elif error and job.attempt < job.max_attempts: job.status = "queued"; job.available_at = current + timedelta(seconds=retry_delay_seconds)
            elif error: job.status = "failed"
            else: job.status = "completed"; job.result_id = result_id
            job.error = error; job.worker_id = None; job.lease_expires_at = None; job.heartbeat_at = None
            if job.status in {"completed", "failed", "cancelled"}: job.finished_at = current
            self._write_job(connection, job)
            if attempt:
                attempt.status = "cancelled" if job.status == "cancelled" else "failed" if error else "completed"
                attempt.error = error; attempt.result_id = result_id; attempt.finished_at = current
                connection.execute("update job_attempts set status=?, data=? where id=?", (attempt.status, attempt.model_dump_json(), attempt.attempt_id))
            return job

    def request_job_cancellation(self, job_id: str):
        with self.conn() as connection:
            connection.execute("begin immediate")
            row = connection.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0])
            if job.status in {"completed", "failed", "cancelled"}: return job
            job.cancellation_requested = True
            if job.status == "queued": job.status = "cancelled"; job.finished_at = now()
            self._write_job(connection, job); return job

    def retry_job(self, job_id: str, allow_completed: bool = False):
        with self.conn() as connection:
            connection.execute("begin immediate")
            row = connection.execute("select data from benchmark_jobs where id=?", (job_id,)).fetchone()
            if not row: return None
            job = BenchmarkJob.model_validate_json(row[0])
            allowed = {"failed", "cancelled"} | ({"completed"} if allow_completed else set())
            if job.status not in allowed: return None
            if job.status == "completed":
                job.result_id = None
                if job.kind == "generate_and_evaluate": job.prediction_id = None
            job.max_attempts = max(job.max_attempts, job.attempt + 1); job.status = "queued"; job.available_at = now()
            job.cancellation_requested = False; job.error = None; job.finished_at = None
            self._write_job(connection, job); return job

    def list_job_attempts(self, job_id: str):
        with self.conn() as connection:
            rows = connection.execute("select data from job_attempts where job_id=? order by attempt", (job_id,)).fetchall()
        return [JobAttempt.model_validate_json(row[0]) for row in rows]

    def dashboard_summary(self):
        with self.conn() as connection:
            instances = connection.execute("select count(*) from benchmark_instances").fetchone()[0]
            predictions = connection.execute("select count(*) from predictions").fetchone()[0]
            jobs = connection.execute("select count(*) from benchmark_jobs where status in ('queued','preparing','generating','evaluating')").fetchone()[0]
            results = connection.execute("select count(*) from evaluation_results").fetchone()[0]
            resolved = connection.execute("select count(*) from evaluation_results where outcome='resolved'").fetchone()[0]
        return {"instances": instances, "predictions": predictions, "active_jobs": jobs, "results": results,
                "resolved": resolved, "resolve_rate": round(resolved / results * 100, 1) if results else 0.0}
