from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from .models import TaskSpec, RunCreate, TestCollection, CollectionRunCreate, CLIENTS, MODEL_NAMES, compose_client_model, enrich_task_metadata
from .storage import Store
from .evaluator import evaluate
from .catalog import search_projects, pull_request_stats
from .adapters import ADAPTERS
import subprocess
app=FastAPI(title="SDD Eval",version="0.1.0"); store=Store()
@app.get("/",response_class=HTMLResponse)
def dashboard(): return (Path(__file__).parent/"dashboard.html").read_text(encoding="utf-8")
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
    task=store.get_task(req.task_id)
    if not task: raise HTTPException(404,"task not found")
    try:
        client_model = compose_client_model(req.client, req.model)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    results = []
    selections = req.models or [req.model]
    if isinstance(selections, str):
        selections = [selections]
    for model in selections:
        selected = compose_client_model(req.client, model)
        result = evaluate(task, req.tool, selected, req.workspace); store.put_run(result); results.append(result)
    return results[0] if len(results) == 1 else {"runs": results, "count": len(results)}

@app.post("/api/runs/compare")
def compare_runs(run_ids: list[str]):
    selected = [r for r in store.list_runs() if r.run_id in run_ids]
    return [{"run_id": r.run_id, "task_id": r.task_id, "model": r.token_usage.provider, "score": r.score, "document": r.metrics.get("document", 0), "code": r.metrics.get("code", 0), "tests": r.metrics.get("tests", 0), "reference": r.metrics.get("reference", 0), "efficiency": r.metrics.get("efficiency", 0)} for r in selected]
