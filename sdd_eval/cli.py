import json, typer, uvicorn
from .models import TaskSpec, TestCollection, compose_client_model, enrich_task_metadata
from .storage import Store
from .evaluator import evaluate
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
