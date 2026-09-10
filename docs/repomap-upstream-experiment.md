# RepoMap 开关对照：Click、Jinja、urllib3

后续已实现生产代码优先、已读范围引导和小地图，并完整重跑同一组任务，见 [导航改造与重测](repomap-navigation-v2.md)。本文件保留改造前结果。

本次核对了实现，并对三个上游真实缺陷进行了真实模型 A/B。没有调整 RepoMap 权重或更换模型样本来追求既定百分比。

## 实现核对

`pico/repo_map.py` 使用 Tree-sitter 提取 Python 符号与静态关系，通过词法相关度构造个性化 PageRank 分布，再用 `0.62 × lexical + 0.36 × graph + kind_boost` 排名。

`ContextManager` 按当前请求生成 RepoMap 并注入模型上下文，地图预算为 1,200 Token，最多选择 24 个符号。静态关系是启发式导航，不能解释成 Python 动态调用关系的完整还原。

## 样本

选择下列已合并上游 PR 的第一父提交作为待修复代码，抽取其原始测试改动放入独立验收副本：

| 任务 | 来源 | 原始提交 | 缺陷 |
| --- | --- | --- | --- |
| Click-2724 | [上游 PR](https://github.com/pallets/click/pull/2724) | c021f05c838c1d0401ebc340d1de9b663c7fb578 | 空字符串默认值不显示在选项帮助中 |
| Jinja-2061 | [上游 PR](https://github.com/pallets/jinja/pull/2061) | 767b23617628419ae3709ccfb02f9602ae9fe51f | overlay 省略异步参数时错误地丢失继承值 |
| urllib3-2998 | [上游 PR](https://github.com/urllib3/urllib3/pull/2998) | 4fb8da2d4e7b7488b432118efe1007d00c81bb53 | 首次 read(0) 触发空缓冲异常 |

任务描述根据公开问题行为整理，不包含实现补丁或目标文件定位。不是 LongSWE-Bench 样本，也不是官方 SWE-bench 评分。

Jinja 的上游新增测试在原始代码上也通过，未覆盖 async 环境调用无参数 overlay 的继承问题。因此在两组验收副本中均加入独立回归 `scripts/fixtures/jinja_overlay_inheritance.py`。原始上游测试保持不变，补充测试不提供给模型。仅使用上游测试的准备结果保存在 `*_upstream_only/`。

原始验收：Click 1 failed / 112 passed；Jinja 1 failed / 34 passed（含补充测试）；urllib3 2 failed / 89 passed。

## 对照条件

- 同一个模型 `gpt-5.6-luna` 和 `https://2btocken.xyz/v1/responses` 接口。
- 每个任务每组一次，共六次；每次从原始提交重新开始，64 轮、1,800 秒。
- 唯一开关为 RepoMap。两组均无预注入代码全文，关闭 Memory、Delegate 与摘要压缩。
- 文件修改只允许生产源码；隐藏测试在工作区外执行，模型不能修改或直接读取它们。
- 使用独立 Python 3.10、本地 macOS 测试环境。Runtime 执行验收，结束后再独立执行一次。
- 工具数按每个实际工具调用计数，包括拒绝、错误和并行调用；模型轮次另计。
- 输入采用后端实际 input_tokens 累计，包括地图开销和缓存 Token，不把 cached_tokens 从输入中扣除。

## 结果

| 任务 | 无 Map 工具数 | 有 Map 工具数 | 无 Map 输入 Token | 有 Map 输入 Token | 最终验收 |
| --- | ---: | ---: | ---: | ---: | --- |
| Click | 6 | 6 | 19,295 | 32,671 | 两组各 113 passed |
| Jinja | 5 | 7 | 22,844 | 33,450 | 两组各 35 passed |
| urllib3 | 15 | 14 | 177,151 | 181,237 | 两组各 91 passed |
| 合计 | 26 | 27 | 219,290 | 247,358 | 六次均 completed 且验收通过 |

`1 - 27 / 26 = -3.85%`：工具数增加 3.85%。

`1 - 247358 / 219290 = -12.80%`：累计输入增加 12.80%。

因此本次没有复现“工具调用减少 17.4%、输入 Token 减少 17.2%”。不能把那两个数字视为本次实现已经验证的收益。

| 辅助指标 | 无 Map | 有 Map |
| --- | ---: | ---: |
| 主模型轮次 | 29 | 22 |
| read/search/list 调用 | 19 | 22 |
| Runtime 完成且独立验收通过 | 3/3 | 3/3 |
| 当前请求与工具配对完整 | 是 | 是 |
| 用量完整 | 是 | 是 |

开启 Map 后轮次减少约 24.1%，但并行段内的读取更多，不能将模型轮数等同于工具调用数。输入反而增加，不支持综合 Token 节省。

## 可观察的问题

首轮地图均选择了 24 个符号。Click 有 20 个来自测试代码，Jinja 有 21 个，urllib3 有 15 个。Click 地图没有直接选出此次修复的 `Option.get_help_record`；Jinja 选中了 `Environment.overlay`，urllib3 选中了 `HTTPResponse.read`，但大量旁支测试和其他读取接口也占据位置。

这提供了后续优化方向：检查测试符号与生产定义的分配、查询词中的通用任务指令，以及排名信息是否真正减少模型补读。当前结果不足以把额外开销全部归因于某一个算法因素；三任务、每组一次存在生成随机性，不能推断 RepoMap 在所有任务上无效。

## 原始证据

项目 `.pico/benchmarks/repomap/summary.json` 保存完整汇总。每个任务的 `without_map/` 和 `with_map/` 包含实际规格、请求用量、模型补丁、验收输出、Session 和 Trace；`with_map/maps.json` 记录每轮实际地图、Token 数和排名明细。

地图实际最大长度分别为 Click 688、Jinja 692、urllib3 886 Token，均在 1,200 预算内。预算是上限，不表示每次必须填满。

入口为 `scripts/repomap_prepare.py`、`scripts/repomap_run.py`、`scripts/repomap_report.py`，复用 `scripts/longswe_verify.py` 的独立验收流程。此次未修改 Pico 生产代码；运行时源码副本保存在原始目录中。凭证未写入脚本或报告。
