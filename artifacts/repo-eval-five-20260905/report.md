# Pico RepoMap 小样本对照

人工选择的 SWE-bench Verified 子集；不是官方全量成绩。缺失 Token 不按 0 处理。
修复成功由离线独立测试判定；Runtime 完成状态单独保留。

| 组别 | 已运行 | 已判分 | 修复成功 | 平均推理秒 | Token 完整记录 |
|---|---:|---:|---:|---:|---:|
| repomap_on | 5 | 5 | 2 | 337.74 | 3 |
| repomap_off | 0 | 0 | 0 | — | 0 |

成对结果：`{"scored_pairs": 0, "on_only_resolved": 0, "off_only_resolved": 0, "both_resolved": 0, "neither_resolved": 0, "mean_seconds_on_minus_off": null, "complete_usage_pairs": 0, "mean_tokens_on_minus_off": null, "pairs_without_generation_errors": 0, "on_only_resolved_without_generation_errors": 0, "off_only_resolved_without_generation_errors": 0}`

样本量小且为人工挑选，结果只用于分析这些任务；不能据此宣称普遍收益或统计显著。
逐任务失败与用量见 results.csv；异常、工具失败与原始调用用量见各 trial 文件。
