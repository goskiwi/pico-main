# LongSWE-Bench 128K 档：Pico 小规模配对实验

后续已按两组统一 64 轮、每任务 30 分钟完整重跑，六次均正常完成并通过验收，见 [充足预算重跑](longswe-128k-budget64.md)。本文件保留原 12 轮预算结果。

本次使用官方 LongSWE-Bench 数据进行实际模型修复，不使用之前的合成历史成绩。参考来源：[数据集](https://huggingface.co/datasets/Steefano/LCB)、[官方代码](https://github.com/Zteefano/long-code-bench)。官方代码检出提交为 `c4039249a76d323b7f8bc7109845e604f95e167e`。

## 样本与环境

从官方 `LongSWE_Bench/128K/test` 选择三个便于在本机运行的不同 Python 仓库任务，各取该 instance 在该档位中上下文最大的条目。选择依据是本地依赖可运行性，不是模型运行成绩。每个任务 raw/managed 各一次，未更换失败样本。

| Instance | 原始提交 | 官方标注 Token | 本地代码 Token 估算 |
| --- | --- | ---: | ---: |
| pytest-dev__pytest-10051 | aa55975c7d3f6c9f6d7f68accc41bb7cadf0eb9a | 123,833 | 122,856 |
| sympy__sympy-24213 | e8c22f6eac7314be8d92590bfff92ced79ee03e2 | 95,356 | 94,538 |
| pallets__flask-5014 | 7ee9ceb71e868944a46e1ff00b506772a53a4f1d | 79,694 | 79,093 |

128K 是官方数据档位，不表示每条输入都恰好 128K。模型实际硬窗口两组均配置为 272,000 Token，为代码、协议、权限列表、历史和 16,000 输出预留空间。不能把本实验写成“在 128K 模型窗口里处理满 128K 输入”。

模型沿用 `gpt-5.6-luna`，接口为 `https://2btocken.xyz/v1/responses`；密钥仅通过环境传入。每次运行最多 12 个主模型轮次、900 秒。实验期间没有修改 Pico 生产代码，源码副本保存在原始记录目录。

## 对照方法

- 两组使用同一 instance、base_commit、官方代码上下文、任务描述、工具、权限和验收测试。
- 将官方 prompt 的代码部分作为可压缩的历史源码上下文，问题描述单独作为保留原文的当前请求。单次 patch 生成任务由此适配成 Pico 多轮文件工具任务，非官方原样推理流程。
- 第一次主请求发送全部提供的代码，不预置 observed 边界；仅在模型真正观察过源码后允许压缩。
- raw 关闭摘要压缩，但保留与 managed 相同的工具级结果预览限制。
- managed 使用现有增量摘要，提前阈值 64K，近期保留目标 4K，摘要生成上限 12K。摘要仍需通过原实现的缩短和整包预算检查。
- 两组使用全新的空记忆文件，并且不使用 Delegate，隔离摘要治理效果。所有失败轮次、补读和重试均保留。
- 逐个主请求检查当前请求的原文，以及原生工具调用与结果的 ID 配对。

## 验收方式

本机没有 Docker，因此使用 macOS 和独立 Python 3.10 环境执行所选任务原始 `test_patch` 中的 FAIL_TO_PASS 与 PASS_TO_PASS 测试节点。这是本地原测试验收，不是官方 Docker harness 或排行榜分数。

隐藏测试放在模型工作区之外的独立评估副本；模型不接收参考修复 patch 或隐藏 test_patch。Runtime 验证时同步候选源码到评估副本，测试文件不被模型修改。每次运行结束还独立执行一次相同节点。

原始代码复现结果：pytest 1 failed / 15 passed，SymPy 1 failed / 31 passed，Flask 1 failed / 59 passed。pytest 环境最初因缺少 pytester 插件报配置错误，显式加载该原生插件后成功复现缺陷；环境错误记录保留，没有把它算成模型失败。

## 实测结果

| 任务 | raw 单轮平均输入 | managed 单轮平均输入 | 降幅 | 最终独立验收（两组） | managed Runtime 结束状态 |
| --- | ---: | ---: | ---: | --- | --- |
| pytest-10051 | 144,850.18 | 19,979.75 | 86.21% | 各 16 passed | 12 轮上限停止 |
| sympy-24213 | 122,487.40 | 35,476.00 | 71.04% | 各 32 passed | completed |
| flask-5014 | 90,259.75 | 19,833.33 | 78.03% | 各 60 passed | completed |

pytest 的 managed 运行在第 12 轮完成最后一次修改后触及上限，未再进入 Runtime 完成验证；结束后独立验收为 16 passed。因此“最终补丁验收通过”为 3/3，但“Runtime 正常完成”为 2/3，不能混为一谈。raw 三个任务均正常完成，其中 pytest 首次修改引入回归，失败反馈后继续修复成功。

| 汇总指标 | raw | managed |
| --- | ---: | ---: |
| 主模型请求 | 20 | 24 |
| 主请求累计输入 Token | 2,566,828 | 571,613 |
| 主请求单轮平均输入 Token | 128,341.40 | 23,817.21 |
| 含摘要的累计输入 Token | 2,566,828 | 904,527 |
| 含摘要的累计输出 Token | 3,363 | 5,195 |
| 工具调用 | 20 | 30 |
| 摘要提交次数 | 0 | 3 |
| 最终补丁独立验收通过 | 3/3 | 3/3 |
| Runtime 正常完成 | 3/3 | 2/3 |
| 当前请求原文与工具配对检查 | 20/20 | 24/24 |
| 后端用量完整 | 是 | 是 |

主指标使用后端 usage.input_tokens，包含缓存部分，不扣除 cached_tokens：

`1 - (571613 / 24) / (2566828 / 20) = 81.44%`

按任务等权平均各自降幅为 78.42%；主请求总输入减少 77.73%；计入摘要后总输入减少 64.76%。这些是不同统计口径，不能互换。摘要增加了输出消耗，缓存命中也不同，因此输入下降不等于费用下降。

工具调用从 20 增至 30；managed 的 SymPy、Flask 轮数与耗时增加，pytest 达到轮次上限。结果支持本样本中输入显著减少，但不支持“无额外代价”或“所有任务均正常完成”。

这次没有得到 46.9%。三个任务、每组一次是探索性样本，不能据此宣称稳定平均收益，更不能将这些数字和以前 12 组合成历史实验混用。

## 原始记录与复现

原始目录：项目 `.pico/benchmarks/longswe/`，包含官方 zip、选择记录、原始提交、各组 workspace/evaluation、依赖环境、源码归档与 `summary.json`。

每个 `instance/{raw,managed}/` 包含 `specification.json`、`requests.json`、`outcome.json`、`result.json`、`model.patch`、验收输出以及 Workspace 内的 Session/Trace。不要对已有输出目录重跑并覆盖证据。

入口：`scripts/longswe_prepare.py`、`scripts/longswe_run.py`、`scripts/longswe_verify.py`、`scripts/longswe_report.py`。准备数据需 `uv run --with pyarrow`；测试环境使用独立 Python 3.10。两个条件的依赖环境共用，源码与测试副本独立。
