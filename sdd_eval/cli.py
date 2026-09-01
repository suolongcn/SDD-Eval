import json, typer, uvicorn
from .models import BenchmarkJob, BenchmarkJobCreate, TaskSpec, TestCollection, compose_client_model, enrich_task_metadata
from .benchmark_io import export_predictions, export_swebench, import_swebench
from .harness import LocalEvaluationBackend
from .docker_backend import DockerEvaluationBackend
from .storage import Store
from .evaluator import evaluate
from .worker import run_workers
app=typer.Typer(no_args_is_help=True)
@app.command()
def import_task(path:str,db:str="sdd_eval.db"):
    payload = json.loads(open(path, encoding="utf-8").read())
    items = payload if isinstance(payload, list) else [payload]
    store = Store(db)
    imported = []
    for item in items:
        task = enrich_task_metadata(TaskSpec.model_validate(item))
        store.put_task(task)
        imported.append(task.id)
    typer.echo("\n".join(imported))
@app.command()
def run(task_id:str,tool:str="openspec",model:str="gpt-5.6-luna",client:str|None="codex",db:str="sdd_eval.db"):
    s=Store(db); task=s.get_task(task_id)
    if not task: raise typer.BadParameter("task not found")
    selected = compose_client_model(client, model)
    result=evaluate(task,tool,selected); s.put_run(result); typer.echo(json.dumps(result.model_dump(),indent=2,default=str))
@app.command()
def serve(host:str="127.0.0.1",port:int=8000): uvicorn.run("sdd_eval.api:app",host=host,port=port)
@app.command()
def collection(id: str, name: str, task_ids: str, db: str = "sdd_eval.db"):
    """Create or replace a collection from comma-separated task IDs."""
    Store(db).put_collection(TestCollection(id=id, name=name, task_ids=[x.strip() for x in task_ids.split(",") if x.strip()]))
    typer.echo(id)

@app.command("import-swebench")
def import_swebench_command(path: str, dataset_id: str, dataset_version: str = "v1", split: str = "dev", db: str = "sdd_eval.db"):
    """Import SWE-bench JSON or JSONL into the isolated Benchmark V2 tables."""
    store = Store(db)
    imported = import_swebench(path, dataset_id=dataset_id, dataset_version=dataset_version, split=split)
    for instance, oracle in imported:
        store.put_benchmark_instance(instance)
        store.put_evaluation_oracle(oracle)
    typer.echo(f"imported {len(imported)} benchmark instances")

@app.command("export-swebench")
def export_swebench_command(output: str, dataset_id: str | None = None, split: str | None = None, include_oracle: bool = False, db: str = "sdd_eval.db"):
    """Export Benchmark V2 JSONL; private oracle fields are excluded by default."""
    store = Store(db)
    instances = store.list_benchmark_instances(dataset_id=dataset_id, split=split)
    oracles = {instance.instance_id: store.get_evaluation_oracle(instance.instance_id) for instance in instances} if include_oracle else None
    export_swebench(output, instances, oracles=oracles, include_oracle=include_oracle)
    typer.echo(f"exported {len(instances)} benchmark instances")

@app.command("export-predictions")
def export_predictions_command(output: str, instance_id: str | None = None, db: str = "sdd_eval.db"):
    """Export stored predictions in SWE-bench prediction JSONL format."""
    store = Store(db)
    predictions = store.list_predictions(instance_id=instance_id)
    export_predictions(output, predictions)
    typer.echo(f"exported {len(predictions)} predictions")

def evaluation_backend(name: str):
    if name == "local": return LocalEvaluationBackend()
    if name == "docker": return DockerEvaluationBackend()
    raise typer.BadParameter("backend must be local or docker")

@app.command("validate-benchmark")
def validate_benchmark_command(instance_id: str, backend: str = "local", workspace: str | None = None, db: str = "sdd_eval.db"):
    """Validate baseline and gold behavior locally for a trusted V2 instance."""
    store = Store(db)
    instance = store.get_benchmark_instance(instance_id)
    oracle = store.get_evaluation_oracle(instance_id)
    if not instance: raise typer.BadParameter("benchmark instance not found")
    if not oracle: raise typer.BadParameter("evaluation oracle not found")
    validation = evaluation_backend(backend).validate_instance(instance, oracle, workspace=workspace)
    store.put_instance_validation(validation)
    typer.echo(json.dumps(validation.model_dump(mode="json"), indent=2))
    if not validation.valid: raise typer.Exit(1)

@app.command("evaluate-prediction")
def evaluate_prediction_command(prediction_id: str, backend: str = "local", workspace: str | None = None, db: str = "sdd_eval.db"):
    """Grade one stored prediction locally; only trusted instances are safe."""
    store = Store(db)
    prediction = store.get_prediction(prediction_id)
    if not prediction: raise typer.BadParameter("prediction not found")
    instance = store.get_benchmark_instance(prediction.instance_id)
    oracle = store.get_evaluation_oracle(prediction.instance_id)
    if not instance: raise typer.BadParameter("benchmark instance not found")
    if not oracle: raise typer.BadParameter("evaluation oracle not found")
    result = evaluation_backend(backend).evaluate(instance, oracle, prediction, workspace=workspace)
    store.put_evaluation_result_v2(result)
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))

@app.command("enqueue-benchmark")
def enqueue_benchmark_command(kind: str, instance_id: str, prediction_id: str | None = None, backend: str = "docker",
                              workspace: str | None = None, max_attempts: int = 3, db: str = "sdd_eval.db"):
    """Queue a durable validate_instance or evaluate_prediction job."""
    store = Store(db)
    try:
        request = BenchmarkJobCreate(kind=kind, instance_id=instance_id, prediction_id=prediction_id,
                                     backend=backend, workspace=workspace, max_attempts=max_attempts)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    if not store.get_benchmark_instance(instance_id): raise typer.BadParameter("benchmark instance not found")
    if prediction_id and not store.get_prediction(prediction_id): raise typer.BadParameter("prediction not found")
    job = BenchmarkJob(**request.model_dump()); store.put_job(job)
    typer.echo(json.dumps(job.model_dump(mode="json"), indent=2))

@app.command("benchmark-worker")
def benchmark_worker_command(db: str = "sdd_eval.db", concurrency: int = 1, lease_seconds: int = 60,
                             poll_seconds: float = 1.0, once: bool = False):
    """Run persistent Benchmark V2 workers independently from the web process."""
    if concurrency < 1: raise typer.BadParameter("concurrency must be at least 1")
    run_workers(db, concurrency, lease_seconds, poll_seconds, once)
