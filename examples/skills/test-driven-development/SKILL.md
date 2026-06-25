---
name: test-driven-development
description: Add features or bug fixes by writing one test first, using TDD and regression tests. 中文: 测试先行, 先写测试, 补测试, 单元测试, 回归测试.
---

# Test Driven Development

Use this when implementing behavior changes.

Do not force this for pure documentation, explanation, or mechanical formatting changes.

Workflow:
- write a focused failing test for the desired behavior
- run that test and confirm it fails for the expected reason
- implement the smallest production change that makes it pass
- rerun the focused test
- run the relevant broader test suite

Keep tests behavioral. Avoid asserting implementation details unless the implementation detail is the contract being changed.
