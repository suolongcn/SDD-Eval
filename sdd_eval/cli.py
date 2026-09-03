import json
from pathlib import Path

import typer
import uvicorn

from .benchmark_io import export_predictions, export_swebench, import_swebench
from .docker_backend import DockerEvaluationBackend
from .harness import LocalEvaluationBackend
from .models import BenchmarkJob, BenchmarkJobCreate, Prediction
from .comparison import build_comparison_report
from .storage import Store
from .worker import run_workers


app = typer.Typer(no_args_is_help=True)

@app.command("comparison-report")
def comparison_report(output: str | None = None, instance_ids: str | None = None, models: str | None = None, db: str = "sdd_eval.db"):
    """Emit a cross-model comparison report for completed evaluations."""
    store = Store(db)
    report = build_comparison_report(store.list_predictions(), store.list_evaluation_results(),
        instance_ids=instance_ids.split(",") if instance_ids else None,
        models=models.split(",") if models else None)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if output: Path(output).write_text(payload + "\n", encoding="utf-8")
    else: typer.echo(payload)


def backend_for(name: str):
    if name == "local": return LocalEvaluationBackend()
    if name == "docker": return DockerEvaluationBackend()
    raise typer.BadParameter("backend must be local or docker")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, db: str = "sdd_eval.db"):
    """Start the V2 dashboard and management API."""
    from . import api
    api.store = Store(db)
    uvicorn.run(api.app, host=host, port=port)


@app.command("import-dataset")
def import_dataset(path: str, dataset_id: str, dataset_version: str = "v1", split: str = "dev", db: str = "sdd_eval.db"):
    """Import SWE-bench-compatible JSON/JSONL, including its private Oracle."""
    store = Store(db)
    imported = import_swebench(path, dataset_id=dataset_id, dataset_version=dataset_version, split=split)
    for instance, oracle in imported:
        store.put_benchmark_instance(instance, oracle)
    typer.echo(f"imported {len(imported)} benchmark instances")


@app.command("export-dataset")
def export_dataset(output: str, dataset_id: str | None = None, split: str | None = None,
                   include_oracle: bool = False, db: str = "sdd_eval.db"):
    """Export public instances by default; --include-oracle is administrator-only."""
    store = Store(db)
    instances = store.list_benchmark_instances(dataset_id=dataset_id, split=split)
    oracles = {item.instance_id: store.get_evaluation_oracle(item.instance_id) for item in instances} if include_oracle else None
    export_swebench(output, instances, oracles=oracles, include_oracle=include_oracle)
    typer.echo(f"exported {len(instances)} benchmark instances")


@app.command("import-predictions")
def import_prediction_file(path: str, db: str = "sdd_eval.db"):
    """Import SWE-bench prediction JSON or JSONL records."""
    text = Path(path).read_text(encoding="utf-8")
    records = json.loads(text) if text.lstrip().startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    store = Store(db); count = 0
    for record in records:
        prediction = Prediction.model_validate(record)
        if not store.get_benchmark_instance(prediction.instance_id):
            raise typer.BadParameter(f"unknown benchmark instance: {prediction.instance_id}")
        store.put_prediction(prediction); count += 1
    typer.echo(f"imported {count} predictions")


@app.command("export-predictions")
def export_prediction_file(output: str, instance_id: str | None = None, db: str = "sdd_eval.db"):
    store = Store(db); predictions = store.list_predictions(instance_id=instance_id)
    export_predictions(output, predictions)
    typer.echo(f"exported {len(predictions)} predictions")


@app.command("validate-instance")
def validate_instance(instance_id: str, backend: str = "docker", workspace: str | None = None, db: str = "sdd_eval.db"):
    """Run baseline/gold validation directly for dataset curation."""
    store = Store(db); instance = store.get_benchmark_instance(instance_id); oracle = store.get_evaluation_oracle(instance_id)
    if not instance: raise typer.BadParameter("benchmark instance not found")
    if not oracle: raise typer.BadParameter("private evaluation oracle not found")
    result = backend_for(backend).validate_instance(instance, oracle, workspace=workspace)
    store.put_instance_validation(result)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    if not result.valid: raise typer.Exit(1)


@app.command("evaluate")
def evaluate_prediction(prediction_id: str, backend: str = "docker", workspace: str | None = None, db: str = "sdd_eval.db"):
    """Evaluate one archived prediction directly."""
    store = Store(db); prediction = store.get_prediction(prediction_id)
    if not prediction: raise typer.BadParameter("prediction not found")
    instance = store.get_benchmark_instance(prediction.instance_id); oracle = store.get_evaluation_oracle(prediction.instance_id)
    if not instance or not oracle: raise typer.BadParameter("instance or private evaluation oracle not found")
    result = backend_for(backend).evaluate(instance, oracle, prediction, workspace=workspace)
    store.put_evaluation_result(result)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))


@app.command("enqueue")
def enqueue(kind: str, instance_id: str, prediction_id: str | None = None, backend: str = "docker",
            workspace: str | None = None, max_attempts: int = 3, db: str = "sdd_eval.db"):
    """Queue validate_instance or evaluate_prediction work."""
    store = Store(db)
    try:
        request = BenchmarkJobCreate(kind=kind, instance_id=instance_id, prediction_id=prediction_id,
                                     backend=backend, workspace=workspace, max_attempts=max_attempts)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    if not store.get_benchmark_instance(instance_id): raise typer.BadParameter("benchmark instance not found")
    if not store.get_evaluation_oracle(instance_id): raise typer.BadParameter("private evaluation oracle not found")
    if prediction_id:
        prediction = store.get_prediction(prediction_id)
        if not prediction or prediction.instance_id != instance_id: raise typer.BadParameter("prediction does not belong to instance")
    job = BenchmarkJob(**request.model_dump()); store.put_job(job)
    typer.echo(json.dumps(job.model_dump(mode="json"), indent=2))


@app.command("worker")
def worker(db: str = "sdd_eval.db", concurrency: int = 1, lease_seconds: int = 60,
           poll_seconds: float = 1.0, once: bool = False):
    """Run persistent V2 workers independently from the web service."""
    if concurrency < 1: raise typer.BadParameter("concurrency must be at least 1")
    run_workers(db, concurrency, lease_seconds, poll_seconds, once)


if __name__ == "__main__":
    app()
