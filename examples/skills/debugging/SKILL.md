---
name: debugging
description: Debug and investigate failing tests, crashes, errors, exceptions, regressions, bugs, or unexpected behavior before changing code. 中文: 调试, 定位问题, 测试失败, 报错, 异常.
---

# Debugging

Understand the failure before editing code.

Use this for broken behavior. Do not use it for routine implementation when no failure has been observed.

Workflow:
- reproduce the symptom with the smallest useful command
- read the failing output carefully
- inspect the code path involved in the failure
- form one hypothesis at a time
- verify the hypothesis with a targeted check
- only then make the smallest fix that addresses the root cause

Avoid broad rewrites until the failure is explained.
