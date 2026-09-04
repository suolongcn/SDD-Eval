from collections import defaultdict
from typing import Any

from .models import EvaluationResult, Prediction


def build_comparison_report(predictions: list[Prediction], results: list[EvaluationResult], *, instance_ids=None, models=None, prediction_ids=None, batch_id=None, run_metadata=None) -> dict[str, Any]:
    allowed_prediction_ids = set(prediction_ids) if prediction_ids is not None else None
    filtered_predictions = [p for p in predictions if allowed_prediction_ids is None or p.prediction_id in allowed_prediction_ids]
    selected_instances = set(instance_ids or [p.instance_id for p in filtered_predictions])
    selected_models = set(models or [p.model_name_or_path for p in filtered_predictions])
    pmap = {p.prediction_id: p for p in filtered_predictions}
    # A retry/re-evaluation can leave an older result for the same prediction;
    # reports must reflect the latest authoritative evaluation only.
    latest_results: dict[str, EvaluationResult] = {}
    for result in results:
        previous = latest_results.get(result.prediction_id)
        if previous is None or result.created_at > previous.created_at:
            latest_results[result.prediction_id] = result
    groups: dict[str, list[EvaluationResult]] = defaultdict(list)
    for result in latest_results.values():
        prediction = pmap.get(result.prediction_id)
        if prediction and prediction.instance_id in selected_instances and prediction.model_name_or_path in selected_models:
            groups[prediction.model_name_or_path].append(result)
    rows = []
    instance_matrix = []
    for model in sorted(selected_models):
        items = groups.get(model, [])
        model_predictions = [pmap[r.prediction_id] for r in items]
        scores = [r.score for r in items]
        usages = [p.token_usage for p in model_predictions]
        rows.append({"model": model, "runs": len(items), "resolved": sum(r.resolved for r in items),
                     "resolve_rate": round(sum(r.resolved for r in items) / len(items), 4) if items else 0,
                     "average_score": round(sum(scores) / len(scores), 2) if scores else 0,
                     "functional_score": round(sum(r.functional_score for r in items) / len(items), 2) if items else 0,
                     "code_quality_score": round(sum(r.code_quality_score for r in items) / len(items), 2) if items else 0,
                     "documentation_score": round(sum(r.documentation_score for r in items) / len(items), 2) if items else 0})
        rows[-1].update({
            "avg_input_tokens": round(sum(u.input_tokens for u in usages) / len(usages), 1) if usages else 0,
            "avg_output_tokens": round(sum(u.output_tokens for u in usages) / len(usages), 1) if usages else 0,
            "total_input_tokens": sum(u.input_tokens for u in usages),
            "total_output_tokens": sum(u.output_tokens for u in usages),
            "total_tokens": sum(u.input_tokens + u.output_tokens for u in usages),
            "avg_total_tokens": round(sum(u.input_tokens + u.output_tokens for u in usages) / len(usages), 1) if usages else 0,
            "avg_latency_ms": round(sum(u.latency_ms for u in usages) / len(usages), 1) if usages else 0,
        })
        for instance_id in sorted(selected_instances):
            matching = [r for r in items if r.instance_id == instance_id]
            latest = max(matching, key=lambda result: result.created_at) if matching else None
            prediction = pmap.get(latest.prediction_id) if latest else None
            metadata = (run_metadata or {}).get((instance_id, model), {})
            instance_matrix.append({
                "instance_id": instance_id,
                "model": model,
                "client": prediction.client if prediction else metadata.get("client"),
                "workflow": prediction.workflow if prediction else metadata.get("workflow"),
                "status": "completed" if latest else metadata.get("status", "pending"),
                "error": None if latest else metadata.get("error"),
                "evaluation_id": latest.evaluation_id if latest else None,
                "outcome": latest.outcome if latest else None,
                "resolved": latest.resolved if latest else False,
                "score": latest.score if latest else None,
            })
    details = []
    for rs in groups.values():
        for result in rs:
            prediction = pmap[result.prediction_id]
            usage = prediction.token_usage
            details.append({"model": prediction.model_name_or_path, "client": prediction.client,
                "workflow": prediction.workflow, "instance_id": result.instance_id,
                "evaluation_id": result.evaluation_id, "outcome": result.outcome,
                "resolved": result.resolved, "score": result.score,
                "functional_score": result.functional_score, "code_quality_score": result.code_quality_score,
                "documentation_score": result.documentation_score,
                "fail_to_pass": {"passed": result.fail_to_pass_passed, "total": result.fail_to_pass_total},
                "pass_to_pass": {"passed": result.pass_to_pass_passed, "total": result.pass_to_pass_total},
                "token_usage": usage.model_dump(mode="json"), "sdd_metrics": result.sdd_metrics,
                "efficiency_metrics": result.efficiency_metrics})
    return {"batch_id": batch_id, "instance_ids": sorted(selected_instances), "models": sorted(selected_models),
            "expected_runs": len(selected_instances) * len(selected_models),
            "total_runs": sum(r["runs"] for r in rows),
            "total_input_tokens": sum(r.get("total_input_tokens", 0) for r in rows),
            "total_output_tokens": sum(r.get("total_output_tokens", 0) for r in rows),
            "total_tokens": sum(r.get("total_tokens", 0) for r in rows),
            "model_comparison": rows,
            "instance_matrix": instance_matrix, "details": details}
