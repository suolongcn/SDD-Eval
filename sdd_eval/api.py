from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from .models import BenchmarkInstance, BenchmarkJob, BenchmarkJobCreate, Prediction, TaskSpec, RunCreate, ComparisonCreate, ComparisonResult, TestCollection, CollectionRunCreate, RunResult, TokenUsage, CLIENTS, MODEL_NAMES, compose_client_model, enrich_task_metadata, now
from datetime import timedelta
from .storage import Store
from .evaluator import evaluate
from .catalog import search_projects, pull_request_stats
from .adapters import ADAPTERS
import subprocess
import threading
import uuid
import time
app=FastAPI(title="SDD Eval",version="0.1.0"); store=Store()
@app.get("/",response_class=HTMLResponse)
def dashboard(): return (Path(__file__).parent/"dashboard.html").read_text(encoding="utf-8")

@app.get("/api/benchmark-instances")
def benchmark_instances(dataset_id: str | None = None, split: str | None = None):
    return store.list_benchmark_instances(dataset_id=dataset_id, split=split)

@app.get("/api/benchmark-instances/{instance_id}")
def benchmark_instance(instance_id: str):
    instance = store.get_benchmark_instance(instance_id)
    if not instance: raise HTTPException(404, "benchmark instance not found")
    return instance

@app.post("/api/benchmark-instances")
def create_benchmark_instance(instance: BenchmarkInstance):
    store.put_benchmark_instance(instance)
    return instance

@app.get("/api/predictions")
def predictions(instance_id: str | None = None):
    return store.list_predictions(instance_id=instance_id)

@app.get("/api/predictions/{prediction_id}")
def prediction(prediction_id: str):
    result = store.get_prediction(prediction_id)
    if not result: raise HTTPException(404, "prediction not found")
    return result

@app.post("/api/predictions")
def create_prediction(prediction: Prediction):
    if not store.get_benchmark_instance(prediction.instance_id):
        raise HTTPException(400, "unknown benchmark instance")
    store.put_prediction(prediction)
    return prediction

@app.get("/api/evaluation-results-v2")
def evaluation_results_v2(instance_id: str | None = None):
    return store.list_evaluation_results_v2(instance_id=instance_id)

@app.get("/api/instance-validations/{instance_id}")
def instance_validation(instance_id: str):
    result = store.get_instance_validation(instance_id)
    if not result: raise HTTPException(404, "instance validation not found")
    return result

@app.get("/api/benchmark-jobs")
def benchmark_jobs(status: str | None = None):
    return store.list_jobs(status=status)

@app.get("/api/benchmark-jobs/{job_id}")
def benchmark_job(job_id: str):
    job = store.get_job(job_id)
    if not job: raise HTTPException(404, "benchmark job not found")
    return job

@app.get("/api/benchmark-jobs/{job_id}/attempts")
def benchmark_job_attempts(job_id: str):
    if not store.get_job(job_id): raise HTTPException(404, "benchmark job not found")
    return store.list_job_attempts(job_id)

@app.post("/api/benchmark-jobs")
def create_benchmark_job(request: BenchmarkJobCreate):
    if not store.get_benchmark_instance(request.instance_id): raise HTTPException(400, "unknown benchmark instance")
    if request.prediction_id:
        prediction = store.get_prediction(request.prediction_id)
        if not prediction or prediction.instance_id != request.instance_id:
            raise HTTPException(400, "prediction does not belong to benchmark instance")
    job = BenchmarkJob(**request.model_dump()); store.put_job(job)
    return job

@app.post("/api/benchmark-jobs/{job_id}/cancel")
def cancel_benchmark_job(job_id: str):
    job = store.request_job_cancellation(job_id)
    if not job: raise HTTPException(404, "benchmark job not found")
    return job

@app.post("/api/benchmark-jobs/{job_id}/retry")
def retry_benchmark_job(job_id: str):
    job = store.retry_job(job_id)
    if not job: raise HTTPException(409, "job is missing or not retryable")
    return job
@app.get("/api/tasks")
def tasks():
    return [enrich_task_metadata(task) for task in store.list_tasks()]
@app.get("/api/capabilities")
def capabilities():
    descriptions = {"openspec": "OpenSpec CLI workflow", "superpowers": "Built-in Superpowers spec-plan-implement-test workflow"}
    client_descriptions = {"codex": "Codex CLI", "opencode": "OpenCode CLI"}
    clients = [{"id": name, "available": True, "description": client_descriptions[name]} for name in CLIENTS]
    models = list(MODEL_NAMES)
    try:
        output = subprocess.run(["opencode.cmd", "models"], capture_output=True, text=True, timeout=20)
        if output.returncode == 0:
            models.extend(line.strip() for line in output.stdout.splitlines() if "/" in line and line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"clients": clients, "models": list(dict.fromkeys(models)), "tools": [{"id": name, "available": True, "description": descriptions.get(name, "Registered SDD adapter") } for name in ADAPTERS]}
@app.get("/api/catalog/projects")
def project_search(provider: str, q: str, limit: int = 10):
    if provider not in {"github", "gitee", "gitcode"}: raise HTTPException(400, "provider must be github, gitee or gitcode")
    return search_projects(provider, q, max(1, min(limit, 50)))
@app.get("/api/catalog/pr-stats")
def pr_stats(provider: str, repo: str, number: int):
    if provider not in {"github", "gitee", "gitcode"}: raise HTTPException(400, "unsupported provider")
    return pull_request_stats(provider, repo, number)
@app.post("/api/tasks")
def create_task(task:TaskSpec):
    enrich_task_metadata(task)
    if task.reference_provider and task.reference_repo and task.reference_pr_number and task.reference_changed_lines is None:
        stats = pull_request_stats(task.reference_provider, task.reference_repo, task.reference_pr_number)
        task.reference_changed_lines = stats.get("changed_lines")
        task.requirement_size = stats.get("size_class", "unknown")
        task.reference_files = stats.get("files")
        task.reference_additions = stats.get("additions")
        task.reference_deletions = stats.get("deletions")
        task.reference_code_lines = task.reference_changed_lines
        task.reference_code_estimated = False
    enrich_task_metadata(task)
    store.put_task(task); return task
@app.post("/api/tasks/import-issue")
def import_issue_task(task: TaskSpec):
    """Import a task whose issue/PR/commit metadata supplies the reference baseline."""
    if not task.source_issue_url or not task.reference_commit:
        raise HTTPException(400, "source_issue_url and reference_commit are required")
    enrich_task_metadata(task)
    store.put_task(task)
    return task
@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str):
    if not store.delete_task(task_id): raise HTTPException(404, "task not found")
    return {"deleted": task_id}
@app.get("/api/collections")
def collections(): return store.list_collections()
@app.post("/api/collections")
def create_collection(collection: TestCollection):
    missing = [task_id for task_id in collection.task_ids if not store.get_task(task_id)]
    if missing: raise HTTPException(400, f"unknown task ids: {', '.join(missing)}")
    store.put_collection(collection); return collection
@app.delete("/api/collections/{collection_id}")
def delete_collection(collection_id: str):
    if not store.delete_collection(collection_id): raise HTTPException(404, "collection not found")
    return {"deleted": collection_id}
@app.post("/api/collections/runs")
def run_collection(req: CollectionRunCreate):
    collection = store.get_collection(req.collection_id)
    if not collection: raise HTTPException(404, "collection not found")
    results = []
    try:
        client_model = compose_client_model(req.client, req.model)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    for task_id in collection.task_ids:
        task = store.get_task(task_id)
        if task:
            result = evaluate(task, req.tool, client_model); store.put_run(result); results.append(result)
    return {"collection_id": collection.id, "count": len(results), "runs": results}
@app.get("/api/runs")
def runs(): return store.list_runs()
@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    result = next((r for r in store.list_runs() if r.run_id == run_id), None)
    if not result: raise HTTPException(404, "run not found")
    archived = store.get_archived_artifacts(run_id)
    if archived:
        # Keep historical artifacts available even after the disposable workspace is removed.
        result.artifacts = {**archived, **(result.artifacts or {})}
    task = store.get_task(result.task_id)
    if task and "reference" not in result.artifacts:
        reference_url = task.reference_url or task.source_issue_url or task.reference_pr_url
        if reference_url:
            result.artifacts["reference"] = {
                "url": reference_url,
                "provider": task.reference_provider,
                "repo": task.reference_repo,
                "code_lines": task.reference_code_lines,
                "code_lines_estimated": task.reference_code_estimated,
            }
    return result
@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str):
    if not store.delete_run(run_id): raise HTTPException(404, "run not found")
    return {"deleted": run_id}
@app.post("/api/runs")
def create_run(req:RunCreate):
    task_ids = req.task_ids or [req.task_id]
    tasks_to_run = [store.get_task(task_id) for task_id in task_ids]
    if any(task is None for task in tasks_to_run): raise HTTPException(404,"task not found")
    try:
        client_model = compose_client_model(req.client, req.model)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    results = []
    selections = req.models or [req.model]
    if isinstance(selections, str):
        selections = [selections]
    for task in tasks_to_run:
      for model in selections:
        selected = compose_client_model(req.client, model)
        run_id = uuid.uuid4().hex[:12]
        pending = RunResult(run_id=run_id, task_id=task.id, status="running", execution_mode="model", generation_status="running", token_usage=TokenUsage(provider=selected, mode="model"), started_at=now())
        store.put_run(pending)
        def execute(rid=run_id, selection=selected, state=pending, run_task=task):
            finished = threading.Event()
            def heartbeat():
                while not finished.wait(2):
                    state.steps = [{"name": "Execute task", "status": "running", "duration_ms": int((now() - state.started_at).total_seconds() * 1000), "detail": "Task is still running; polling for latest output."}]
                    state.metrics["last_progress"] = now().isoformat()
                    store.put_run(state)
            thread = threading.Thread(target=heartbeat, daemon=True); thread.start()
            try:
                result = evaluate(run_task, req.tool, selection, req.workspace)
                result.run_id = rid
                store.put_run(result)
            except Exception as error:
                state.status = "failed"
                state.generation_status = "failed"
                state.error = str(error)
                state.finished_at = now()
                state.duration_ms = int((state.finished_at - state.started_at).total_seconds() * 1000)
                state.steps = [{"name": "Run error", "status": "failed", "duration_ms": state.duration_ms, "detail": str(error)}]
                store.put_run(state)
            finally:
                finished.set()
        threading.Thread(target=execute, daemon=True).start()
        results.append(pending)
    if len(results) == 1: return results[0]
    return {"runs": results, "count": len(results), "average_score": None}

@app.post("/api/runs/compare")
def compare_runs(run_ids: list[str]):
    selected = [r for r in store.list_runs() if r.run_id in run_ids]
    return [{"run_id": r.run_id, "task_id": r.task_id, "model": r.token_usage.provider, "score": r.score, "document": r.metrics.get("document", 0), "code": r.metrics.get("code", 0), "tests": r.metrics.get("tests", 0), "reference": r.metrics.get("reference", 0), "efficiency": r.metrics.get("efficiency", 0)} for r in selected]

def comparison_view(comparison: ComparisonResult):
    by_id = {run.run_id: run for run in store.list_runs()}
    selected = [by_id[run_id] for run_id in comparison.run_ids if run_id in by_id]
    active = any(run.status in {"queued", "running"} for run in selected)
    finished = [run for run in selected if run.status not in {"queued", "running"}]
    scores = [run.score for run in finished if run.score is not None]
    status = "running" if active else "failed" if selected and all(run.status == "failed" for run in selected) else "completed"
    return {
        **comparison.model_dump(mode="json"),
        "status": status,
        "completed_runs": len(finished),
        "total_runs": len(comparison.run_ids),
        "score": round(sum(scores) / len(scores), 2) if scores else None,
        "runs": [{
            "run_id": run.run_id, "task_id": run.task_id, "model": run.token_usage.provider,
            "status": run.status, "score": run.score, "started_at": run.started_at,
            "scoring_basis": run.scoring_basis, "metrics": run.metrics,
        } for run in selected],
    }

@app.get("/api/comparisons")
def comparisons():
    return [comparison_view(item) for item in store.list_comparisons()]

@app.get("/api/comparisons/{comparison_id}")
def comparison_detail(comparison_id: str):
    comparison = store.get_comparison(comparison_id)
    if not comparison: raise HTTPException(404, "comparison not found")
    return comparison_view(comparison)

@app.post("/api/comparisons")
def create_comparison(req: ComparisonCreate):
    if not req.task_ids or not req.models: raise HTTPException(400, "task_ids and models are required")
    response = create_run(RunCreate(task_id=req.task_ids[0], task_ids=req.task_ids, models=req.models, model=req.models[0], client=req.client, tool=req.tool, workspace=req.workspace))
    pending = response.get("runs", []) if isinstance(response, dict) else [response]
    comparison = ComparisonResult(comparison_id=uuid.uuid4().hex[:12], task_ids=req.task_ids, models=req.models, run_ids=[run.run_id for run in pending])
    store.put_comparison(comparison)
    return comparison_view(comparison)
