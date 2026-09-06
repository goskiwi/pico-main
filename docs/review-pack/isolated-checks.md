# 可选的容器内复现检查

`run_check` 让模型在提交之前运行一个临时 Python／pytest 片段，检查需求复现和可能的行为回归。
它通过现有 ToolRuntime 的准入、工具事务、日志与回放运行。检查成功不是任务完成证明；固定
公开 verifier 和独立验收仍分别执行。

## 启用方式与范围

程序化构造显式传入 `Pico(..., check_runner=isolated_backend.run_check)` 才安装工具。
默认没有执行器时，工具表中没有 run_check；Ask 模式也不暴露它。普通 CLI 默认不自动安装 Docker
执行器。当前评测脚本注入 `DockerPublicVerifier.run_check`，因此 Code／Auto 可以使用。

执行器是受信任的应用依赖，必须提供隔离执行；不能把宿主 Python／Shell 执行函数冒充隔离后端。
Core 不依赖 Docker，也不把任意传入的 callable 自动变成沙箱。

模型参数：

- `code`：最多 16,000 字符，Python 代码或定义 pytest 测试及 fixture 的代码。
- `kind`：python 或 pytest。
- `timeout_seconds`：默认 30 秒，允许 1～60 秒，实际不能超过 Run 剩余预算。

pytest 片段放在调用方配置的测试目录，适用该目录的 conftest 与 fixture；路径由应用配置，
模型不能选择宿主执行目录。临时文件只存在于本次容器。

## 执行与资源行为

每次运行使用当前 workspace 的补丁和全新容器。宿主源码、实际 Git index 与正式测试文件保持不变。
没有宿主挂载、宿主 API key 或外网，复用已准备的镜像；调用中不下载镜像。容器资源沿用公开
verifier 的 2 CPU、4 GiB 和 512 进程限制。

诊断输出最多保存 256 KiB，超限会停止检查并返回明确失败。外层使用已有 CommandRunner 的
Deadline／取消信号，正常结束、超时、取消和输出超限后都清理本次容器。容器内还使用独立 timeout
限制脚本执行，避免宿主进程异常结束后脚本无限运行；宿主被强杀时，停止后的容器记录仍可能需清理。

run_check 不能与读取工具组成并行 batch。它不会写入“验证通过”事实，失败也不会自动转化为一套
新的完成约束：模型需要判断是实现错误还是诊断本身不成立，修复后可以再次检查；可信的固定验证
继续由 CompletionController 执行。不要把模型自行设计的断言当作独立验收标准。

## 已完成的验证（没有 LLM 调用）

通过新工具执行“setup 中持有日志列表引用，进入 call 后仍应保留 setup 记录”的检查：

| 代码 | run_check 结果 |
|---|---|
| 历史原代码 | 通过 |
| 上一轮模型候选补丁 | 失败，检出列表被清空 |
| 数据集参考修复 | 通过 |

三个检查均通过真实 Docker 与工具事务执行，宿主文件未改变，日志回放一致。
另验证了容器内改写测试文件不会影响宿主、宿主 API key 不进入检查环境、超时、父级取消、
输出上限、真实退出码和容器清理。独立容器 timeout 也进行了实际执行检查。

这只证明工具能够运行并检测给定的反例，**还没有证明模型会主动设计这个检查，或修复成功率会提高**。
上述能力验收不调用 LLM；后续真实任务另行记录，历史候选补丁和验收记录未修改。

## 复现本地集成检查

准备并验证 pytest-dev__pytest-10051 的源码和镜像后：

```bash
PICO_TEST_DOCKER=1 uv run pytest -q tests/test_checks_docker.py
```

默认 pytest 不运行需要预备镜像的 Docker 检查；普通准入、模式、批次、完成权等测试始终运行。
Docker 检查不调用模型、不下载镜像。边界场景代码位于
`benchmarks/repo_eval/checks/retained_setup_reference.py`，它是人工审查发现的开发回归，不是新的盲测题。

本地记录：`artifacts/repo-eval-checks-local-final/acceptance.json`。

## 接下来的真实验证

只做一个真实任务，使用通常的问题描述，不提前把这段边界测试塞给模型；记录模型是否主动调用
run_check、设计了什么检查、如何处理失败，再进行公开验证和独立验收。该已审查案例只能作为
开发样本。若模型没有检查到或仍有回归，应保留失败并分析，不能宣称工具增加就保证修复正确。
完成这个验证后，再选择未用于协议开发的任务评估整体效果。
