from sdd_eval.generator import AgentGenerator
from sdd_eval.models import BenchmarkInstance, RequirementIR
import subprocess


def test_generation_prompt_contains_public_instance_and_selected_sdd_workflow():
    instance = BenchmarkInstance(
        instance_id="demo__repo-1", repo="demo/repo", base_commit="abc",
        problem_statement="Fix the public behavior.",
        requirements=[RequirementIR(id="REQ-1", description="Return the expected value.", acceptance_criteria=["The public test passes"])],
    )

    prompt = AgentGenerator._prompt(instance, "openspec")

    assert "Fix the public behavior" in prompt
    assert "REQ-1" in prompt and "OpenSpec" in prompt
    assert "hidden evaluator tests" in prompt


def test_agent_commands_preserve_client_and_model_selection(tmp_path):
    generator = AgentGenerator()
    codex = generator._agent_command(tmp_path, "codex", "gpt-5.6-sol", "do it")
    opencode = generator._agent_command(tmp_path, "opencode", "gpt-5.6-luna", "do it")

    assert "codex" in codex[0] and codex[codex.index("--model") + 1] == "gpt-5.6-sol"
    assert codex[1:3] == ["--profile", "relay"]
    assert "--dangerously-bypass-approvals-and-sandbox" in codex
    assert "--approve-for-me" not in codex and "--sandbox" not in codex
    assert "opencode" in opencode[0] and opencode[opencode.index("--model") + 1] == "gpt-5.6-luna"


def test_openspec_change_name_collapses_consecutive_separators():
    name = AgentGenerator._change_name("spring-guides__gs-spring-boot-healthz")

    assert name == "benchmark-spring-guides-gs-spring-boot-healthz"
    assert "--" not in name


def test_generation_prompt_limits_changes_to_instance_working_directory():
    instance = BenchmarkInstance(instance_id="demo", repo="demo", base_commit="abc", problem_statement="Fix it")
    instance.environment.working_directory = "complete"

    prompt = AgentGenerator._prompt(instance, "openspec")

    assert "working directory is `complete`" in prompt
    assert "Do not edit sibling tutorial stages" in prompt
    assert "Do not create or modify test files" in prompt


def test_documents_are_collected_from_the_agent_working_directory(tmp_path):
    root = tmp_path / "repo"; agent_root = root / "complete"
    document = agent_root / "openspec" / "changes" / "demo" / "design.md"
    document.parent.mkdir(parents=True); document.write_text("design", encoding="utf-8")
    sibling = root / "openspec" / "wrong.md"
    sibling.parent.mkdir(parents=True); sibling.write_text("wrong", encoding="utf-8")

    documents = AgentGenerator()._documents(root, agent_root, "openspec")

    assert documents == {"complete/openspec/changes/demo/design.md": "design"}


def test_openspec_documents_are_completed_without_entering_model_patch(tmp_path):
    instance = BenchmarkInstance(instance_id="demo__case", repo="demo", base_commit="abc", problem_statement="Fix it")
    instance.environment.working_directory = "complete"
    agent_root = tmp_path / "repo" / "complete"; agent_root.mkdir(parents=True)

    AgentGenerator()._ensure_documents(agent_root, instance, "openspec")
    documents = AgentGenerator()._documents(tmp_path / "repo", agent_root, "openspec")

    assert any(name.endswith("proposal.md") for name in documents)
    assert any(name.endswith("design.md") for name in documents)
    assert any(name.endswith("tasks.md") for name in documents)


def test_patch_excludes_sdd_and_client_files_inside_working_directory(tmp_path):
    root = tmp_path / "repo"; complete = root / "complete"
    (complete / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (complete / "src" / "App.java").write_text("class App {}\n", encoding="utf-8")
    (complete / "src" / "test").mkdir()
    (complete / "src" / "test" / "AppTest.java").write_text("class AppTest {}\n", encoding="utf-8")
    (complete / ".codex" / "skills").mkdir(parents=True)
    (complete / ".codex" / "skills" / "SKILL.md").write_text("generated", encoding="utf-8")
    (complete / "openspec").mkdir()
    (complete / "openspec" / "proposal.md").write_text("proposal", encoding="utf-8")
    instance = BenchmarkInstance(instance_id="demo", repo="demo", base_commit="abc", problem_statement="Fix")
    instance.environment.working_directory = "complete"

    patch = AgentGenerator()._patch(root, instance)

    assert "complete/src/App.java" in patch
    assert "complete/.codex" not in patch and "complete/openspec" not in patch
    assert "complete/src/test" not in patch


def test_patch_filter_removes_workflow_and_test_diff_sections():
    patch = """diff --git a/src/main.py b/src/main.py
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1 @@
-old
+new
diff --git a/openspec/changes/demo/design.md b/openspec/changes/demo/design.md
--- a/openspec/changes/demo/design.md
+++ b/openspec/changes/demo/design.md
@@ -0,0 +1 @@
+design
diff --git a/src/test/test_main.py b/src/test/test_main.py
--- a/src/test/test_main.py
+++ b/src/test/test_main.py
@@ -0,0 +1 @@
+test
"""

    filtered = AgentGenerator._filter_patch(patch)

    assert "src/main.py" in filtered
    assert "openspec/" not in filtered
    assert "src/test/" not in filtered
