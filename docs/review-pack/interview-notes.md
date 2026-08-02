# pico 面试讲稿

## 30 秒项目介绍

`pico` 是一个本地 coding agent runtime，重点不是接通 LLM，而是解决三个工程问题：
如何在有限 token 内选对上下文，如何让工具调用持续、受控且可恢复，以及如何在不破坏用户 Git
状态的前提下撤销一次 Agent 修改。

我的主线是“上下文选择 → 受控执行 → 可审计恢复”；真实模型 fixture 与隐藏 verifier 用来验证
这些设计，而不是只展示 demo。

## 最值得讲的亮点：任务相关上下文，而不是历史拼接

直接把仓库全文或整段聊天历史塞进 prompt 会浪费 token，也会让干扰实现稀释注意力。`pico` 用
真实 `tiktoken` 计数分配预算：Repo Map 用 tree-sitter 构建符号图与 Personalized PageRank，
工作记忆保存当前任务事实；跨轮的完整证据保存在 session 与 run artifacts 中，只在继续任务时
按需引用近期运行，而不在默认请求前做长期记忆检索。

文件事实只保留为带内容 hash 的短摘要，文件变化后立即失效；失败、拒绝等过程事实才进入有上限的
process notes。session 只保存最新可恢复 checkpoint，完整 checkpoint 历史仍在对应 run artifact，
不会把“记忆”做成另一份无界聊天记录。

它既能自动注入每轮上下文，也暴露只读 `query_repo_map`，让模型对新的子问题重新排序；源码真相
仍通过工具读取，而完整过程证据则留在可审计的运行工件中。

Skill 同样按需加载：初始 prompt 只包含 `name`、`description` 和路径，模型需要时才读完整
`SKILL.md`。metadata 对齐 Agent Skills 的小写 kebab-case 和必填描述；带
`disable-model-invocation` 的审查流程不会自动暴露给模型，只能由用户 `/skill <name>` 显式启用。
Pico 额外允许严格 Skill 只收缩工具 schema，因此 Skill 指令不能扩大本地权限边界。

另一个关键取舍是：Mermaid task canvas 只做 UI、审计和 checkpoint 的下钻导航；它不会替代
provider 会话中的原始 tool result。这样 patch 与测试修复仍基于精确证据，而画布可以阶段折叠。

## 第二亮点：连续工具会话与任务内压缩

Responses API 的加密 reasoning、function call 与匹配 function output 会原样回放到同一会话，不会每个工具调用后重新拼
一份“历史 prompt”。运行时同时观察 provider 返回的 input token：到达阈值或 provider 明确报告
context overflow 时，才用同一模型把任务历史编译成固定结构的 checkpoint，并保留最近原始工具证据。
checkpoint 绑定当时 workspace 指纹、记录 verifier 状态和 artifact 引用；provider 会话重置后，Pico
从 checkpoint、最新 Repo Map 与当前 workspace 继续。压缩次数有每任务上限，生成失败会透明停止而不是静默降级。

归档的 V5 证据是在此前的 `gpt-5.4` 上采集，保留为历史基线；当前默认模型为
`gpt-5.6-luna`，并有同一冻结 V2 十题的独立 1× 结果。

## 第三亮点：可恢复但不改写 Git

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

1. `RepoMap`、会话工作记忆与任务状态由 `ContextManager` 按真实 token 预算组装。
2. 面向 GPT-5.6 Luna 的窄 Responses 适配层把 strict function calling 归一为
   `ModelAction(tool | final | retry)`，工具输出保留在 provider 会话。
3. `agent_loop` 执行有界状态转换；接近 token 阈值或明确溢出时，生成 task checkpoint 后继续。
4. `tools.py` 用同一份 Pydantic model 做 schema、提示展示和本地校验。
5. 高风险动作经过 capability、approval 和危险命令检查；`run_shell` 只能进无网络、只读 RootFS、
   资源受限的 Docker。
6. task state、trace、完整工具输出、阶段折叠的 canvas 和 Undo journal 分层落盘。

## 面试官可能追问

### 为什么 provider strict schema 之后还要本地校验？

Provider 只负责输出形状，不能替代本地权限边界。调用可能来自兼容端点、重放 artifact 或未来的
其他入口；路径保护、read-only、approval 和危险命令规则都必须由 runtime 自己执行。

### 为什么不做多 provider / 多模型适配层？

Pico 的目标是可审计的单用户 coding runtime，不是模型网关。当前只针对 GPT-5.6 Luna 的
Responses contract 做窄适配：隔离 SDK message 和函数调用协议，使 runtime、测试与审计工件不直接
依赖 SDK 类型；但不维护模型目录、OAuth、能力协商或跨 API 转译。后者会显著扩大测试矩阵，却不能
改善当前任务的工具安全与恢复语义。

### 为什么画布不直接替代即时上下文？

画布是低 token 的任务导航和审计视图，适合折叠旧步骤；但 patch、测试失败和冲突恢复需要完整
源码与原始工具输出。即时上下文优先由 provider 会话保存精确证据；需要压缩时，checkpoint 只保存
结构化状态和 artifact 引用，并额外保留一小段最近原始证据，模型仍可按需下钻。

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

1. 展示 `pico/context_manager.py` 的预算和 `pico/repo_map.py` 的 symbol graph 排序入口。
2. 展示一次 run 的连续 tool output、`task.mmd`、`phases/`、`offload.jsonl` 和 `refs/`。
3. 展示 `pico/tools/` 的单一 Pydantic schema 和 `pico/sandbox.py`。
4. 展示 `undo/manifest.json`，再用 reliability 报告说明脏文件恢复和 digest 验证。
5. 用 V5 预算门槛说明为什么保持动态默认，而不是追求更小数字。
6. 最后主动说明本地单用户与微基准的外部有效性边界。
