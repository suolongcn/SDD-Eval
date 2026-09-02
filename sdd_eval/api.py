from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .models import BenchmarkInstance, BenchmarkJob, BenchmarkJobCreate, GenerationJobCreate, Prediction
from .storage import Store


app = FastAPI(title="SDD Eval", version="2.0.0")
store = Store()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(
        (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/summary")
def summary():
    return store.dashboard_summary()


@app.get("/api/generation-capabilities")
def generation_capabilities():
    import shutil
    return {
        "clients": [{"id": name, "available": bool(shutil.which(name) or shutil.which(f"{name}.cmd"))} for name in ("codex", "opencode")],
        "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"],
        "workflows": [{"id": "openspec", "available": bool(shutil.which("openspec") or shutil.which("openspec.cmd"))}, {"id": "superpowers", "available": True}],
    }


@app.get("/api/instances")
def instances(dataset_id: str | None = None, split: str | None = None):
    return store.list_benchmark_instances(dataset_id=dataset_id, split=split)


@app.get("/api/instances/{instance_id}")
def instance(instance_id: str):
    value = store.get_benchmark_instance(instance_id)
    if not value: raise HTTPException(404, "benchmark instance not found")
    return value


@app.post("/api/instances")
def create_instance(value: BenchmarkInstance):
    if value.docker.build_context or value.docker.dockerfile or value.docker.pull:
        raise HTTPException(400, "image build and pull settings are administrator-only")
    store.put_benchmark_instance(value)
    return value


@app.delete("/api/instances/{instance_id}")
def delete_instance(instance_id: str):
    if not store.delete_benchmark_instance(instance_id): raise HTTPException(404, "benchmark instance not found")
    return {"deleted": instance_id}


@app.get("/api/predictions")
def predictions(instance_id: str | None = None):
    return store.list_predictions(instance_id=instance_id)


@app.get("/api/predictions/{prediction_id}")
def prediction(prediction_id: str):
    value = store.get_prediction(prediction_id)
    if not value: raise HTTPException(404, "prediction not found")
    return value


@app.post("/api/predictions")
def create_prediction(value: Prediction):
    if not store.get_benchmark_instance(value.instance_id): raise HTTPException(400, "unknown benchmark instance")
    store.put_prediction(value)
    return value


@app.get("/api/results")
def results(instance_id: str | None = None, prediction_id: str | None = None):
    return store.list_evaluation_results(instance_id=instance_id, prediction_id=prediction_id)


@app.get("/api/results/{evaluation_id}")
def result(evaluation_id: str):
    value = store.get_evaluation_result(evaluation_id)
    if not value: raise HTTPException(404, "evaluation result not found")
    return value


@app.get("/api/validations")
def validations(instance_id: str | None = None):
    return store.list_instance_validations(instance_id=instance_id)


@app.get("/api/validations/latest/{instance_id}")
def latest_validation(instance_id: str):
    value = store.get_instance_validation(instance_id)
    if not value: raise HTTPException(404, "instance validation not found")
    return value


@app.get("/api/jobs")
def jobs(status: str | None = None, instance_id: str | None = None):
    return store.list_jobs(status=status, instance_id=instance_id)


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    value = store.get_job(job_id)
    if not value: raise HTTPException(404, "benchmark job not found")
    return value


@app.get("/api/jobs/{job_id}/attempts")
def job_attempts(job_id: str):
    if not store.get_job(job_id): raise HTTPException(404, "benchmark job not found")
    return store.list_job_attempts(job_id)


@app.post("/api/jobs")
def create_job(request: BenchmarkJobCreate):
    if request.backend != "docker":
        raise HTTPException(400, "HTTP jobs require the isolated docker backend; use CLI for trusted local execution")
    if not store.get_benchmark_instance(request.instance_id): raise HTTPException(400, "unknown benchmark instance")
    if not store.get_evaluation_oracle(request.instance_id): raise HTTPException(400, "instance has no private evaluation oracle")
    if request.prediction_id:
        selected = store.get_prediction(request.prediction_id)
        if not selected or selected.instance_id != request.instance_id:
            raise HTTPException(400, "prediction does not belong to benchmark instance")
    value = BenchmarkJob(**request.model_dump())
    store.put_job(value)
    return value


@app.post("/api/generations")
def create_generation(request: GenerationJobCreate):
    if not store.get_benchmark_instance(request.instance_id):
        raise HTTPException(400, "unknown benchmark instance")
    if not store.get_evaluation_oracle(request.instance_id):
        raise HTTPException(400, "instance has no private evaluation oracle")
    value = BenchmarkJob(
        kind="generate_and_evaluate", instance_id=request.instance_id,
        backend=request.backend, workspace=request.workspace,
        max_attempts=request.max_attempts, client=request.client,
        model=request.model, workflow=request.workflow,
    )
    store.put_job(value)
    return value


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    value = store.request_job_cancellation(job_id)
    if not value: raise HTTPException(404, "benchmark job not found")
    return value


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    current = store.get_job(job_id)
    allow_completed = False
    if current and current.kind == "generate_and_evaluate" and current.status == "completed" and current.result_id:
        result = store.get_evaluation_result(current.result_id)
        allow_completed = bool(result and not result.resolved)
    value = store.retry_job(job_id, allow_completed=allow_completed)
    if not value: raise HTTPException(409, "job is missing or not retryable")
    return value
