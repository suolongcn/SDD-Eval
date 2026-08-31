from sdd_eval.models import TaskSpec, enrich_task_metadata


def test_repository_metadata_is_available_without_reference_pr():
    task = TaskSpec(
        id="example",
        title="Small change",
        repository="https://github.com/example/project",
        requirements=[{"id": "R1", "description": "Do one thing"}],
    )

    enrich_task_metadata(task)

    assert task.reference_url is None
    assert task.reference_provider == "github"
    assert task.reference_repo == "example/project"
    assert task.reference_code_lines is not None
    assert task.reference_code_estimated is True
    assert task.requirement_size == "small"


def test_linked_change_lines_are_authoritative():
    task = TaskSpec(
        id="example",
        title="Referenced change",
        repository="https://gitee.com/example/project",
        reference_changed_lines=640,
    )

    enrich_task_metadata(task)

    assert task.reference_url is None
    assert task.reference_code_lines == 640
    assert task.reference_code_estimated is False
    assert task.requirement_size == "medium"


def test_issue_reference_takes_priority_over_repository_and_pull_request():
    task = TaskSpec(
        id="example",
        title="Issue-backed change",
        repository="https://github.com/example/project",
        source_issue_url="https://github.com/example/project/issues/42",
        reference_pr_url="https://github.com/example/project/pull/43",
    )

    enrich_task_metadata(task)

    assert task.reference_url == "https://github.com/example/project/issues/42"
