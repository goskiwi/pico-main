# pico 面试讲稿

## 30 秒项目介绍

`pico` 是一个本地 coding agent runtime，重点不是接通 LLM，而是解决三个工程问题：
模型如何在大于上下文窗口的仓库里找到正确代码，模型动作如何被限制在可审计边界内，以及一次
Agent 修改如何在不破坏用户 Git 状态的前提下恢复。

我的主线是 Repo Map、Docker-only 执行边界和 Run Undo；真实模型 fixture 与隐藏 verifier 用来
验证这些设计，而不是只展示 demo。

## 最值得讲的亮点：Task-aware Repo Map

直接把仓库全文塞进 prompt 会浪费 token，也会让干扰实现稀释注意力。`pico` 用 tree-sitter 提取
类、函数、方法和签名，再把 import、调用、继承、包含和测试关系组成加权图。用户请求中的符号和
文件线索作为 Personalized PageRank 的 personalization vector，最后只渲染预算内的相关签名。

它既能自动注入每轮上下文，也暴露只读 `query_repo_map`，让模型对新的子问题重新排序。实现只
支持 Python，没有正则降级；这是为了让边界和失败方式清楚。

真实 V4 A/B 专门构造了多 package、跨模块调用和 `legacy/experiments` 同名干扰文件。clean
commit 上三轮结果是 `full` 13/15、`no_repo_map` 6/15。这个结果说明 Repo Map 在目标使用场景
有价值，同时报告也保留了额外 token 和时延成本。

## 第二亮点：可恢复但不改写 Git

自动 commit + `git reset` 会污染用户分支、index 和已有脏文件。`pico` 改为 run-level Undo：

```text
写操作前记录首触前像
  -> 执行工具
  -> 记录实际变化路径与运行后状态
  -> Undo 时全量冲突预检
  -> 全部安全才逐路径恢复
```

如果 Agent 结束后用户又修改了任一路径，整次恢复拒绝，不会恢复一半。原本已经 dirty 的文件会
回到 Agent 开始前的 dirty 内容，而不是回到 `HEAD`。

真实可靠性回归中，Repo Map 定位 3/3，两个 Undo 场景 6/6 恢复，完整 workspace digest 6/6
回到运行前状态，原有脏 README 3/3 被保留。

## 2 分钟架构讲解

1. `RepoMap` 刷新符号图，`ContextManager` 按预算组 prompt。
2. Responses strict function calling 返回统一 `ModelAction(tool | final | retry)`。
3. `agent_loop` 执行有界状态转换。
4. `tools.py` 用同一份 Pydantic model 做 schema、提示展示和本地校验。
5. 高风险动作经过 capability、approval 和危险命令检查。
6. `run_shell` 只能进无网络、只读 RootFS、资源受限的 Docker。
7. task state、trace、report、完整工具输出和 Undo journal 分层落盘。

## 面试官可能追问

### 为什么 provider strict schema 之后还要本地校验？

Provider 只负责输出形状，不能替代本地权限边界。调用可能来自兼容端点、重放 artifact 或未来的
其他入口；路径保护、read-only、approval 和危险命令规则都必须由 runtime 自己执行。

### 为什么还要 LangGraph，普通 while 循环不够吗？

普通循环完全可以实现当前控制流。这里使用 LangGraph 的价值是把 model、tool、retry、final
转换显式化，便于以后增加分支；但 durable state、checkpoint、trace 和恢复语义仍由 Pico 自己
维护，而不是把框架的内存状态当作持久化边界。如果项目规模继续保持当前大小，移除 LangGraph
也是合理的依赖简化方向。

### Repo Map 会不会把同名调用连错？

会。当前实现是轻量静态近似，不做完整类型推断或动态分派分析；同名符号、运行时导入和反射都会
产生歧义。项目通过 import、containment、测试关联和词法命中共同排序，并保留 `query_repo_map`
与文件工具供模型二次确认。它解决的是预算内的候选定位，不声称构建精确调用图。

### 为什么不把 Repo Map 预算直接降到 600？

V5 预先规定：600 cap 至少 13/15、不能低于动态预算、每次成功尝试成本至少下降 5%，才改默认。
实际两边都是 14/15，但成本只下降 3.36%，所以没有为了一个看起来更小的数字改默认。这说明项目
会用门槛做工程决策，而不是看到一次正结果就上线。

### Docker 就等于安全吗？

不是。这里的边界是本地执行 containment：无网络、只读容器 RootFS、capability drop 和资源限制。
Workspace 仍需可写，Docker daemon、本地模型端点、多租户身份和供应链安全都在项目边界之外。

### 这是不是生产级 Agent 平台？

不是。它是本地单用户 runtime。生产化仍需要远程隔离池、多租户身份与配额、集中式 secret
manager、持久队列、幂等任务、监控告警和供应链策略。

## 3 分钟演示顺序

1. 展示 `pico/repo_map.py` 的 symbol graph 和排序入口。
2. 展示 V4 fixture 的 active path 与干扰 path，再展示 13/15 vs 6/15。
3. 展示 `pico/tools.py` 的单一 Pydantic schema 和 `pico/sandbox.py`。
4. 展示一次 run 的 `trace.jsonl`、`task.mmd`、`offload.jsonl` 和 `undo/manifest.json`。
5. 用 reliability 报告说明脏文件恢复和 digest 验证。
6. 最后主动说明本地单用户与微基准的外部有效性边界。
