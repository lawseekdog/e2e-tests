---
name: legal_consultation
description: 法律咨询场景 - 租赁押金纠纷咨询
service_type: legal_consultation
url: http://localhost:5175
credentials:
  username: admin
  password: admin123456
steps_file: steps.yaml
---

# 法律咨询 - 租赁押金纠纷

## 案情描述

当事人张三与房东李四签订房屋租赁合同，支付押金2000元。租期届满退租时，房东以墙面污损为由拒绝退还押金。当事人认为墙面污损属于正常使用磨损，房东拒退押金不合理。

**当事人诉求**：要求房东退还押金2000元，并承担合理维权费用。

**已有材料**：
- 租赁合同
- 房屋交接清单
- 与房东的聊天记录

## 证据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 咨询材料 | assets/consult_note.txt | 案情概述和已有材料说明 |

## 测试步骤

> **注意**: 结构化测试步骤定义在 `steps.yaml` 文件中，供 Chrome DevTools MCP 自动化执行。

详见 [steps.yaml](./steps.yaml)

## 预期产物

- `docs/verification.png` - 咨询结果验证截图

## 验收标准

- [ ] 会话创建成功
- [ ] AI 返回咨询意见
- [ ] 响应内容包含法律分析
- [ ] 截图保存成功

## Quality Check Expectations

```yaml
memory:
  retrieval:
    - entity_key: "party:client:primary"
      must_include: ["张三"]
    - entity_key: "party:opponent:primary"
      must_include: ["李四", "房东"]
    - entity_key: "amount:deposit"
      must_include: ["2000"]
    - entity_key: "issue:main"
      must_include: ["押金", "退还"]
  storage:
    - entity_key: "party:client:primary"
      scope: case
      expected_value_contains: "张三"
    - entity_key: "consultation:topic"
      scope: case
      expected_value_contains: "租赁押金"

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["租赁", "押金", "合同法"]
    - query_type: "case_reference"
      must_match_count: ">= 0"

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        service_type: "legal_consultation"
    - table: "matter_tasks"
      count: ">= 1"

skills:
  executed:
    - skill_id: "consultation-intake"
      status: "completed"
    - skill_id: "legal-analysis"
      status: "completed"

trace:
  expectations:
    - span_name: "run_skill"
      count: ">= 2"
    - span_name: "llm_call"
      count: ">= 3"

phase_gates:
  checkpoints:
    - phase: "intake"
      status: "completed"
      required_outputs: ["profile.client", "profile.issue"]
    - phase: "analysis"
      status: "completed"
      required_outputs: ["consultation_response"]

document:
  quality:
    format:
      not_applicable: true
    style:
      legal_terms_check: true
      formal_language: true
    content:
      must_include:
        - "押金"
        - "租赁"
        - "合同"
        - "建议"
      must_not_include:
        - "{{.*}}"
        - "TODO"
```
