import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from .models import TaskSpec, RunResult, ComparisonResult, now, enrich_task_metadata

def datetime_from_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
class Store:
    def __init__(self, path: str = "sdd_eval.db"):
        self.path = path; Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c: c.executescript("create table if not exists tasks (id text primary key, data text not null, created_at text not null); create table if not exists runs (id text primary key, task_id text not null, data text not null, created_at text not null); create table if not exists run_artifacts (run_id text primary key, documents text not null default '{}', generated_code text not null default '', repository_code text not null default '', created_at text not null); create table if not exists collections (id text primary key, data text not null, created_at text not null); create table if not exists comparisons (id text primary key, data text not null, created_at text not null);")
        self.migrate_legacy_runs()
        self.migrate_run_artifacts()
    def conn(self):
        c = sqlite3.connect(self.path); c.row_factory = sqlite3.Row; return c
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
