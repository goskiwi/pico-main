# 五题真实仓库修复评测（2026-09-05）

**5 个预定试次和独立判分全部结束：2 个通过既定独立验收、1 个修复不完整、1 个预算耗尽、1 个模型调用异常。**

按预定试次统计为 2/5；其中 Astropy 没有获得有效模型响应，不能将该失败解释为模型无法解决题目。
这是一组人工预选任务上的本地单组实验，不是官方全量成绩，也没有验证 RepoMap 收益。

| 任务 | Runtime 结果 | 独立验收 | Run 秒 | 逻辑模型调用 | 输入＋输出 Token |
|---|---|---|---:|---:|---:|
| pytest-dev__pytest-5787 | completed | 通过 | 396.602 | 16 | 317,841 |
| pytest-dev__pytest-7571 | completed | 通过 | 138.515 | 7 | 80,373 |
| sphinx-doc__sphinx-8548 | completed | 未通过；目标行为缺失 | 366.830 | 16 | 345,240 |
| psf__requests-6028 | stopped / turn_timeout | 未通过；无补丁 | 600.020 | 26 | 缺失；已知 622,666 |
| astropy__astropy-12907 | 未返回终态 / 调用异常 | 未通过；无有效模型响应 | 186.731 | 1 | 缺失 |

用量完整的三题共 743,454 Token；Requests 已上报 622,666 Token，Astropy 无已上报用量。
已知输入合计 1,334,708，输出 31,412，相加 1,366,120；另有 2 次调用缺少用量，不能给出完整总量或五题平均 Token。
pytest-5787 的接口 total_tokens 合计为 317,842，比输入加输出多 1；原字段与差额均保留。
66 是逻辑模型调用数，不是 HTTP 传输尝试次数；供应商内部重试未返回的用量也无法推算。

Run 耗时合计 1,688.698 秒（约 28.1 分钟），包含运行中的工具和公开验证；不含环境准备及独立判分。
独立判分耗时合计 39.122 秒，部分与后续题的模型作答重叠；两者相加不是整轮墙钟时间。

## 过程核对

- 每题只有一个 Run、一条初始用户任务；无 user_guidance，未在作答期追加提示或手改候选。
- 三个 completed 与一个 stopped 结果均与日志回放一致；Astropy 的异常调用未写入终态，日志仍可回放为 running，不冒充完成。
- 五个 Run 都没有未闭合的工具调用；Astropy 没有实际执行工具。
- 五份 candidate.patch 与 predictions.jsonl 及当前工作区内容一致；变化路径未包含测试或依赖配置。
- 三次完成提交的公开验证均通过。Requests 与 Astropy 只执行了作答前公开检查，没有完成提交后的公开验证。
- 本轮没有根据独立判分修改候选或重开试次；历史试次未与本轮合并计算。

“作答期人工介入为零”不包含人工选题、配置验证、准备环境和事后分析，也不证明不存在所有潜在回归。
原始结果见 [results.csv](../../artifacts/repo-eval-five-20260905/results.csv)，判分见 [judgments.json](../../artifacts/repo-eval-five-20260905/judge/judgments.json)。

## 样本与条件

人工预选 SWE-bench Verified 的五道题，覆盖四个 Python 仓库；不是随机抽样或官方全量成绩。
选题前检查已有运行记录，排除已作答的 pytest-10051、pytest-10356、Sphinx-7748，以及独立验收环境无效的 Requests-2931。
未按模型成绩选题，作答期间不追加提示、不手改补丁、不重跑挑选结果。

| 任务 | 问题 | 原代码公开检查 |
|---|---|---|
| pytest-5787 | 异常链序列化 | reports：14 通过 |
| pytest-7571 | 日志级别恢复 | logging：57 通过 |
| Sphinx-8548 | 继承属性文档 | autoattribute/autoclass：16 通过 |
| Requests-6028 | 代理认证 | 无需网络服务的代理与认证检查：14 通过 |
| Astropy-12907 | 嵌套模型可分离性 | separable/compound：74 通过、6 跳过 |

模型 gpt-5.6-luna，温度 0；单 Agent、Auto、RepoMap 开启、允许容器内临时复现。
每题一次，最多 32 轮、64 次工具执行、600 秒 Run 时间、单次输出 4096 Token。
本轮只运行一个组，不计算 RepoMap 收益。Token 是接口上报消耗，不是供应商账单。

公开检查在原仓库已有测试中根据问题涉及子系统选定，没有读取参考补丁或数据集 test_patch 来设计。
模型在运行中接收公开检查反馈；独立验收在作答结束后运行，不反馈给当前试次。

Astropy 原代码首次公开检查有 6 项因 NumPy 数组转标量弃用警告被提升为异常而失败。
正式运行前，仅将该条警告设为 default，使警告正常显示；保留所有测试及断言、原始源码、依赖和独立验收方式。
初次失败及调整后结果均保留。这不是模型修复，也不计作模型试次。

运行前本地回归：231 项通过；显式 Docker 集成：5 项通过。

## Sphinx-8548 失败分析

Runtime 返回 completed，公开的 16 项测试通过；模型另外执行了语法解析和 3 项既有继承成员测试，也通过。
独立验收中 test_inherited_instance_variable 失败：生成文档遗漏继承的 Bar.attr1；5 项既有回归测试通过。

结合失败输出与候选代码，缺口在成员发现：补丁只在 Documenter 中添加按 MRO 查找属性文档的逻辑，
而 importer.get_class_members 追加源码分析得到的实例属性时仍只匹配当前类命名空间。
未进入成员集合的继承实例属性，无法靠后续文档查找修复。
模型的临时检查未新增这一行为场景，已有公开测试也未覆盖它。

该题记为 independent_tests_failed。保留原始补丁与成绩，没有把独立测试反馈给模型后重试。
这是当前公开检查覆盖不足与模型修复不完整的实例，不是运行环境故障。

## Requests-6028 失败分析

模型在固定 600 秒预算内持续检查代理连接与依赖实现，没有生成补丁，也没有请求最终公开验证。
Runtime 因 turn_timeout 停止。独立验收的 2 项目标测试仍失败，属于预算内未完成修复，不能当作环境故障排除。
最后一次调用约 12 秒后以连接相关 RuntimeError 结束，Runtime 当时已到 Run deadline，按 turn_timeout 收尾。
该调用未返回用量；总 Token 为缺失，已返回调用的用量单列，不按零补齐。

## Astropy-12907 调用异常

原代码公开检查已通过，首个逻辑模型调用约 180 秒后以 RuntimeError 结束，错误信息为
“Could not reach the OpenAI-compatible backend.”。没有收到有效动作、没有执行工具、没有补丁。
整个 Run 区间记录为 186.731 秒，包括调用前 Runtime 准备。
独立验收仍实际执行空补丁：2 项目标测试失败、13 项既有回归通过；验证环境有效。
这是模型调用异常，不能据此推断模型的解题能力。缺失用量保留为 null，没有重试解题挑选结果。
该试次结束后，Provider 错误记录已改为保留传输异常类型、重试次数、HTTP 状态与服务端错误信息；
历史试次保持原样，不用新实现补写无法恢复的底层原因。

## 简历表述边界

可写：在 4 个开源仓库的 5 个预选任务上执行真实模型修复与独立验收，保留逐调用用量、工具日志及失败分类；2 个试次通过既定独立验收。
不能写成“通用修复率 40%”“RepoMap 提升成功率／降低 Token”或“5 道题均完成修复”。
后续若修复评测暴露的问题，应另开实验并保留本轮原始结果。

## 复现

```bash
uv run python scripts/run_repo_eval.py run \
  --catalog artifacts/repo-eval-five-preparation-20260905/catalog.json \
  --ids pytest-dev__pytest-5787 pytest-dev__pytest-7571 sphinx-doc__sphinx-8548 psf__requests-6028 astropy__astropy-12907 \
  --variants repomap_on --model gpt-5.6-luna \
  --output artifacts/repo-eval-five-NEW-RUN

tmp/swebench-env/bin/python scripts/repo_eval_tasks.py judge \
  --predictions artifacts/repo-eval-five-NEW-RUN/predictions.jsonl \
  --output artifacts/repo-eval-five-NEW-RUN/judge

uv run python scripts/run_repo_eval.py summarize \
  --output artifacts/repo-eval-five-NEW-RUN \
  --judgments artifacts/repo-eval-five-NEW-RUN/judge/judgments.json
```

复现需要本地已准备的源码和镜像、项目模型配置，并会产生模型 API 费用；使用新的输出目录。

## 证据位置

- 预选任务与首次公开命令：artifacts/repo-eval-five-preparation-20260905/selection.json
- 最终运行任务配置：同目录 catalog.json
- Astropy 环境调整：同目录 environment-adjustment.json
- 本轮条件、结果、判分、候选补丁与审计：artifacts/repo-eval-five-20260905/ 中纳入版本库的文件
- 本轮源码：同目录 runtime-source.zip、runtime-git.json；包含未提交工作区，不冒充已提交版本。
- 逐 Run 回放、补丁和用量核对：同目录 process-audit.json

逐次模型请求和容器测试的完整日志保留在本地评测目录，不纳入版本库；提交中的紧凑证据保留
运行条件、汇总、逐题判分、候选补丁、过程审计和当时的 Runtime 源码快照。
