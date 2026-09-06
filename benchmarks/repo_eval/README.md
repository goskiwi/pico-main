# Pico 真实仓库测试协议

当前协议为 **public-feedback-v3**，在 v2 的公开验证反馈上增加可选的容器内复现工具。
它测试 Auto 模式的单 Agent 核心运行链，包括真实的
CompletionController 和运行中验证。Child 编排与压缩收益分别测试，不能混进 RepoMap 对照。
旧的 `repo-eval-pilot-20260905` 使用无测试反馈协议，保留作为失败分析材料，不能与 v2 合并计算成绩。
v2 原始结果也保持独立，不能与增加了工具的 v3 混算。

`run_check` 允许模型运行临时 Python／pytest 片段来检验需求与边界；不会修改宿主源码或正式测试。
它默认限时 30 秒，最多 60 秒，并受 Run 剩余预算约束；单次输出限制 256 KiB，失败结果反馈给模型。
它不替代固定 verifier 或独立验收。只有配置了容器执行器的试次才获得此工具。
能力验证记录见 `artifacts/repo-eval-checks-local-final/acceptance.json`；这不是 LLM 成绩。

## 先回答三个不同的问题

| 问题 | 测法 | 能得出的结论 |
|---|---|---|
| Runtime 是否按规则运行 | 确定性动作、真实文件修改、真实验证命令、日志回放 | 失败是否阻止完成、修复后是否继续、取消是否停止 |
| 接上真实模型能否修复 | 完整单 Agent 流程，公开回归反馈，独立验收 | 固定条件下的修复成功数与失败原因 |
| RepoMap 有没有收益 | 在上一行正常运行后，仅切换 RepoMap，成对执行同一组任务 | 该样本上的成功、调用量和用时差；不预设存在提升 |

预设模型动作只用于第一行，永远不算真实模型成绩。运行中验证通过也不等于独立验收通过。

## 每次真实试次如何运行

1. 从历史 base commit 创建独立 workspace。公开验证命令事先写在任务配置里，不由模型决定。
2. 在原代码上实际运行公开回归测试。如果环境／公开测试不成立，该试次不调用模型，批次停止。
3. 配置 `verification_command`，由原有 Runtime 创建 `verify_changes=true` 的 TaskContract。
4. 模型通过 read/search/edit 等工具工作。每次 `submit_final` 后，Runtime 在全新 Docker 容器中
   应用当前改动并执行公开测试。失败输出回到模型，继续修复；通过才允许完成。
5. 作答结束后，在另一全新容器应用最终补丁和数据集独立验收测试，判断 FAIL_TO_PASS 与
   PASS_TO_PASS 是否满足。参考补丁、独立测试和裁判日志不进入模型 workspace，也不反馈给该试次。

公开测试使用**原仓库已有测试**，不读取数据集 `test_patch` 来生成它们；本轮两道已分析的题是
协议开发样本，不能冒充未见测试集。新需求的测试覆盖仍可能不足，最终成功只由独立验收决定。

Pico 仍使用原有 CompletionController、Verification 和 Deadline；评测只注入
`DockerPublicVerifier` 适配现有 CommandRunner 接口，没有在评测脚本中另写一套完成判定。
生成候选补丁使用临时 Git index，不改变实际 workspace 的 index，避免验证自身被记录成文件副作用。

容器无主机目录挂载、无 API key、禁网，限制 2 CPU、4 GiB、512 个进程。验证执行受当前 Run
剩余时间与取消信号约束，结束后清理本次容器。只使用已准备镜像，真实试次中不隐式下载大镜像。

## 现有任务的准确状态

任务来自 [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified)。
`tasks.json` 保存数据集修订和各仓库 base commit；这是人工选题，不是随机抽样或官方排行榜成绩。
公开历史题可能进入过模型训练数据，不能把成绩全部归因于 Runtime。

- 候选 12 题：pytest 4、Sphinx 4、Requests 2、Astropy 2。
- 独立验收环境可运行 11 题。Requests-2931 的参考修复仍有网络相关回归失败，保留为环境问题。
- v2 的协议开发题为 **pytest-10051、Sphinx-7748**；pytest-10356 后续也已单独作答。
- [五题真实评测](../../docs/review-pack/repo-eval-five-2026-09-05.md)预选 pytest-5787、pytest-7571、
  Sphinx-8548、Requests-6028、Astropy-12907，作答前实际验证全部公开检查。
  5 个试次中 2 个通过独立验收，另有修复不完整、预算耗尽、模型调用异常各 1 个；仅运行 RepoMap 开启组。
- 尚未作答的有效候选为 Sphinx-9229、Sphinx-9658、Astropy-13579，仍需配置／验证公开检查。
  公开检查应先根据原仓库功能范围选定并验证，不能按模型成绩换题或按参考补丁暴露目标实现位置。

## 执行次序与预算

首先完成无付费模型的运行链检查：原代码公开测试通过 → 已知错误补丁被 Runtime 拒绝 → 修复后
通过 → Runtime 完成。另用 Sphinx 旧补丁确认：公开测试通过仍可能独立验收失败。
本地检查记录位于 `artifacts/repo-eval-v2-local/acceptance.json` 和
`artifacts/repo-eval-failure-analysis/public-tests.json`；这些都不算 LLM 成绩。

单题协议检查示例（五题实验的固定条件和完整命令见上方报告）：

```bash
uv run python scripts/run_repo_eval.py run \
  --ids pytest-dev__pytest-10051 --variants repomap_on \
  --model gpt-5.6-luna --output artifacts/repo-eval-v3-real-20260905
```

命令会产生 API 费用。模型 key 和 endpoint 从项目环境配置读取，输出目录必须是新目录。
默认 32 个 Agent Turn、64 次工具执行、600 秒 Run 总预算、每次输出最多 4096 Token；
上下文上限 65536，压缩预留 8192，保留近期历史 16384。公开验证时间包含在 Run 预算内。
设置／模型调用异常会结束批次，不自动启动剩余试次或重开同一题挑选最好结果。
每次逻辑模型调用内部保留现有 Provider 的最多三次传输尝试，不等同于三次解题机会。

独立判分与汇总：

```bash
tmp/swebench-env/bin/python scripts/repo_eval_tasks.py judge \
  --predictions artifacts/repo-eval-v3-real-20260905/predictions.jsonl \
  --output artifacts/repo-eval-v3-real-20260905/judge

uv run python scripts/run_repo_eval.py summarize \
  --output artifacts/repo-eval-v3-real-20260905 \
  --judgments artifacts/repo-eval-v3-real-20260905/judge/judgments.json
```

这一次只验证真实模型运行链，不用来比较 RepoMap。之后的对照应使用显式确定的样本、同一模型和
endpoint、同一工具集合和预算；两组各自独立 checkout、Session、Run Log，交替运行顺序。
先各跑一次，只有运行与记录正常、预算明确时才对整个样本统一增加重复，不能只重跑失败题。
所有 `run` 必须显式指定 `--ids`，不会默认启动 12 题或 66 次请求的批量实验。

## 怎么记成绩

- **修复成功**：独立验收通过且未违规修改测试／依赖配置；同时单列 Runtime 是否完成。
- **公开反馈**：每次验证的通过／失败、耗时、完整日志，和 Run Log 内的 verification_result。
- **耗时**：Run 总耗时包含模型、工具和运行中验证；模型调用边界耗时与验证耗时另存。
- **Token**：主 Agent 和压缩模型的接口上报用量；缺失为 null，另列已知量及缺失调用数。
  `total_tokens` 为输入与输出相加；`provider_total_tokens` 单列接口总量，差异保留在 `provider_total_delta`。
  这不是实际账单，传输重试未返回的用量不能推断为 0。
- **人工介入**：每个作答试次不加提示、不手工修补；环境准备和脚本开发不计作模型试次干预。
- **失败分类**：环境／设置错误、模型调用异常、预算停止、公开测试失败、独立验收失败等分开保留。
  任意异常都不允许被静默删掉、自动重试后只留最好答案。
- **对照结论**：只比较条件一致的成对任务。模型调用异常和用量缺失应明确标注，不能据此声称
  RepoMap 提升了能力或降低了成本。两个开发样本不能代表整个 SWE-bench 或所有真实仓库。

`experiment.json` 保存条件，`runtime-source.zip` 保存实际运行源码；每个试次保存候选补丁、调用
用量、公开验证日志和完整 `.pico` 日志。旧协议的记录保持原样。本地探索记录工作区未提交状态，
不冒充某个已提交版本的正式验收；既有 `run_real_*` 的干净提交要求保持不变。

## 重新准备环境

```bash
uv venv tmp/swebench-env --python 3.11
uv pip install --python tmp/swebench-env/bin/python -r benchmarks/repo_eval/requirements.txt

tmp/swebench-env/bin/python scripts/repo_eval_tasks.py prepare \
  --output artifacts/repo-eval-controls

tmp/swebench-env/bin/python scripts/repo_eval_tasks.py validate \
  --output artifacts/repo-eval-controls
```

独立验收的环境检查实际执行原代码与参考修复，分别检查目标失败和修复后通过。它与公开验证是
两件不同的事。环境失败保留日志，不修改成功标准，也不更换成模型更容易通过的题。
