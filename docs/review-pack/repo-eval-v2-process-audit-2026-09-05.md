# 真实试次的逐过程复核（2026-09-05）

本次对照原始 requests.json、43 条事件、运行时源码归档、三个公开验证记录和独立判分记录。
没有再调用 LLM。结论：运行与反馈顺序一致，但首次修改有错误，第二次修改仍有额外回归；
此前“修复成功”的表述应收紧为“通过既定验收”。

## 六次逻辑模型调用

| 次数 | 秒 | 输入 Token | 输出 Token | 接口 total_tokens | 行为与检查结论 |
|---|---:|---:|---:|---:|---|
| 1 | 5.998 | 2759 | 143 | 2902 | 2 次读文件、2 次搜索，正确执行，无修改 |
| 2 | 5.857 | 6215 | 138 | 6353 | 再次读取清理逻辑、阶段测试和 stash 用法，4 个观察调用全部成功 |
| 3 | 7.291 | 13368 | 284 | 13652 | 将 reset 中创建新列表改为就地清空；执行成功，但修改破坏阶段隔离 |
| 4 | 67.546 | 14344 | 154 | 14499 | 进入完成判断，真实公开测试 1 失败、14 通过；Runtime 拒绝完成并反馈 |
| 5 | 24.845 | 9864 | 1064 | 10928 | 在阶段结束时把记录复制进 stash；修复了已暴露问题，但没有保护调用方先前持有的列表 |
| 6 | 67.574 | 11409 | 80 | 11489 | 再次请求完成；公开测试 15 项通过，Runtime 随后记录终态 |

两批观察均为 4 个独立只读调用，随后两次修改独占各轮。共 10 个普通工具执行，验证命令和完成
判断不混计为模型调用。两次 edit 的 expected_revision 均与前态匹配，无 revision 冲突。

事件 27、37 的两次 edit 可以从历史 base 源码重建出当前最终文件，字节内容一致；Run Log replay
也得到 completed。日志无 user_guidance 或 resume，已记录的两次工具修改解释了最终文件改动。
“人工干预 0”限于本试次没有追加人工提示或手改补丁，不包括人工设计评测、选择任务和准备环境。

## 时间口径

- 182.861 秒是 Run 区间，包括两次运行中公开验证；不是整轮准备到独立验收完成的墙钟时间。
- 六次模型调用边界累计 179.111 秒，其余共 3.750 秒，包括 Runtime、本地工具、公开验证和清理。
- 两次完成请求的模型调用合计 135.120 秒，约占 Run 时间 73.9%。这是模型调用边界的耗时，不能
  当成 pytest 耗时，也无法仅凭当前计时分解为模型推理、服务端排队、传输或内部重试。
- 原代码预检另记 0.953 秒，且该计时不含适配器 finally 清理；独立验收另记 9.810 秒。
- 源码克隆、前置准备以及两段命令之间的等待没有统一的全程计时，不应声称“端到端只花 183 秒”。

## Token 口径修正

输入合计 57,959，输出合计 1,863，二者相加为 **59,822**；接口 total_tokens 字段合计却为
**59,823**。差异来自第 4 次响应：14344 + 154 = 14498，但接口给出 14499。

不能把计算值当成完全一致的原始接口总量。当前统计代码已分开保留 total_tokens（输入加输出）、
provider_total_tokens（接口总量）和 provider_total_delta（差额），并增加回归测试。历史原始
requests.json 与 result.json 未改写，复核值另存在 artifacts/repo-eval-v2-audit/audit.json。

6 是逻辑 complete_action 调用数，不是已审计的 HTTP 请求次数。Provider 内部最多允许三次传输
尝试；未保存逐次传输信息。接口用量不是供应商账单，也不是 59,822 个不重复的上下文 Token。

## 独立验收的结论边界

原有验收确实通过：1 项 FAIL_TO_PASS、15 项 PASS_TO_PASS 全部满足。公开 verifier 不应用
数据集 test_patch；独立裁判在模型作答结束后才应用它，相关文件不在模型 workspace。

但候选补丁仍存在未被这些断言覆盖的回归。追加的行为检查如下：

```python
@pytest.fixture
def setup_records(caplog):
    logging.warning("setup message")
    return caplog.get_records("setup")


def test_retained_setup_records(caplog, setup_records):
    assert [r.getMessage() for r in setup_records] == ["setup message"]
    logging.warning("call message")
    assert [r.getMessage() for r in setup_records] == ["setup message"]
```

测试使用原代码、原始候选补丁、数据集参考修复分别运行于全新容器；没有应用原数据集 test_patch：

| 代码 | 结果 |
|---|---|
| 历史原代码 | 通过 |
| 本次模型候选补丁 | 失败：进入 call 阶段后 setup_records 已变为 [] |
| 数据集参考修复 | 通过 |

原因是：模型只把 stash 内的列表换成副本；调用方在 setup 阶段已经拿到的旧列表仍然是 handler
使用的那份列表。进入 call 阶段时 reset 就地清空，调用方保存的 setup 记录跟着丢失。复制 stash
条目解决了后续重新查询的情况，没有保护已经返回给调用方的记录。

这是事后代码审查发现的额外行为回归，不篡改已有基准成绩。该补丁应先处理这个问题，再重新检查
新测试、公开回归与独立验收；不能直接作为无回归的修复交付。

## 证据

- 原始运行：artifacts/repo-eval-v2-real-20260905/pytest-dev__pytest-10051__r1__repomap_on/
- 复核汇总：artifacts/repo-eval-v2-audit/audit.json
- 额外检查代码：artifacts/repo-eval-v2-audit/test_retained_reference.py
- 额外检查结果：artifacts/repo-eval-v2-audit/additional-check.json
- 原代码、候选与参考修复日志：同目录 original.log、candidate.log、reference.log

此次只纠正统计与报告口径，保留原始候选与成绩；没有通过重跑模型挑选更好的结果。
