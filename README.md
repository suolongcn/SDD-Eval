# SDD Eval

面向 OpenSpec 等 SDD 工具的可扩展评测系统 MVP，支持 SQLite、FastAPI Dashboard、本机执行、OpenAI-compatible/本地模型和文档/代码/测试/效率评分。

```powershell
pip install -e .
sdd-eval import-task examples/tasks/issue-backed-open-source.json
sdd-eval run python-requests-issue-6512
sdd-eval serve
```

客户端和 SDD 工作流可独立选择。客户端支持 `codex` 和 `opencode`，工作流支持 `openspec` 和 `superpowers`：

```powershell
sdd-eval run python-requests-issue-6512 --client opencode --model gpt-5.6-luna --tool openspec
sdd-eval run python-requests-issue-6512 --client codex --model gpt-5.6-sol --tool superpowers
```

也可直接使用 `--model codex` 或 `--model opencode`；带 `:model` 后缀可传递客户端模型名。

首批 Issue 基准任务：

```powershell
sdd-eval import-task examples/tasks/petclinic-issue-2600.json
sdd-eval import-task examples/tasks/java-design-patterns-issue-3576.json
sdd-eval import-task examples/tasks/tutorials-issue-19173.json
```

这些任务来自公开 GitHub Issue，并记录了 Issue、已合并 PR 和参考 commit（若公开 API 可获得）。评测运行会保存基准元数据；后续可增加参考 commit checkout、diff 相似度和行为等价性评分。

浏览器访问 `http://127.0.0.1:8000`。当前 OpenSpec Adapter 和 Mock Provider 用于验证编排链路；后续可替换为真实 OpenSpec CLI 和模型端点。

`mock` 是干跑模式：它只验证下载、编排、构建和测试链路，不调用模型、不生成实现，Token 为 0，运行状态为 `incomplete`，质量分为 0。要获得真实文档、代码和质量分，请在看板中填写 OpenAI-compatible 服务 URL，并设置 `SDD_EVAL_MODEL`（API Key 可通过 `SDD_EVAL_API_KEY` 设置）。

仓库下载支持 GitHub 和 Gitee。执行时先尝试 `git clone`，失败后自动尝试仓库 ZIP（Gitee 使用 `repository/archive/{revision}.zip`）。例如：

```powershell
sdd-eval import-task examples/tasks/issue-backed-open-source.json
sdd-eval run python-numpy-issue-32453
```
