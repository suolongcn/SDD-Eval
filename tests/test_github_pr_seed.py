import re

from scripts.seed_github_prs import TASKS, changed_lines, oracle_test_patch


def test_github_pr_seed_has_requested_size_distribution_and_metadata():
    assert len(TASKS) == 10
    assert len({(task["repo"], task["pr"]) for task in TASKS}) == 10
    assert sum(500 <= task["lines"] <= 2000 for task in TASKS) == 6
    assert sum(task["lines"] < 500 for task in TASKS) == 4
    assert all(re.fullmatch(r"[0-9a-f]{40}", task["base_commit"]) for task in TASKS)
    assert all(re.fullmatch(r"[0-9a-f]{40}", task["reference_commit"]) for task in TASKS)


def test_seed_line_counter_excludes_diff_headers():
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-OLD_VALUE = 1
+NEW_VALUE = 2
"""

    assert changed_lines(patch) == 2


def test_seed_builds_separate_hidden_fail_and_pass_checks():
    task = {"pr": 123}
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-OLD_VALUE = 'unchanged'
+NEW_VALUE = 'official merged behavior'
"""

    test_patch, fail_test, pass_test, target = oracle_test_patch(task, patch)

    assert target == "app.py"
    assert fail_test == ".sdd_eval_tests/pr_123_fail.py"
    assert pass_test == ".sdd_eval_tests/pr_123_pass.py"
    assert "official merged behavior" in test_patch
    assert test_patch.count("diff --git") == 2
