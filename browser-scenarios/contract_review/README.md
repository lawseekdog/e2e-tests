---
name: contract_review
description: 合同审查场景 - 采购合同风险审查
service_type: contract_review
url: http://localhost:5175
credentials:
  username: admin
  password: admin123456
steps_file: steps.yaml
---

# 合同审查 - 采购合同风险审查

## 案情描述

客户（甲方：北京甲方科技有限公司）拟与供应商（乙方：上海乙方供应链有限公司）签订服务器设备采购合同。合同金额200,000元，客户希望在签约前对合同条款进行法律审查，识别潜在风险并提出修改建议。

**审查重点**：
1. 违约责任条款是否合理
2. 免责声明是否对甲方不利
3. 争议解决方式是否对甲方有利
4. 付款条件和交付条款是否平衡

## 证据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 采购合同 | assets/sample_contract.txt | 待审查的采购合同文本 |

## 测试步骤

> **注意**: 结构化测试步骤定义在 `steps.yaml` 文件中，供 Chrome DevTools MCP 自动化执行。

详见 [steps.yaml](./steps.yaml)

## 预期产物

- `docs/verification.png` - 审查结果验证截图

## 验收标准

- [ ] 会话创建成功
- [ ] 合同文件上传成功
- [ ] AI 返回审查意见
- [ ] 审查意见识别出违约金过高风险
- [ ] 审查意见识别出免责条款风险
- [ ] 审查意见识别出仲裁条款风险
- [ ] 审查意见包含修改建议
- [ ] 截图保存成功

## Quality Check Expectations

```yaml
memory:
  retrieval:
    - entity_key: "party:client:primary"
      must_include: ["甲方", "北京甲方科技"]
    - entity_key: "party:counterparty:primary"
      must_include: ["乙方", "上海乙方供应链"]
    - entity_key: "amount:contract:total"
      must_include: ["200000", "20万"]
    - entity_key: "risk:identified"
      must_include: ["违约金", "免责", "仲裁"]
  storage:
    - entity_key: "party:client:primary"
      scope: case
      expected_value_contains: "甲方"
    - entity_key: "contract:type"
      scope: case
      expected_value_contains: "采购"
    - entity_key: "risk:penalty_clause"
      scope: case
    - entity_key: "risk:disclaimer_clause"
      scope: case
    - entity_key: "risk:arbitration_clause"
      scope: case

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["合同法", "违约责任", "免责条款"]
    - query_type: "contract_template"
      must_match_count: ">= 0"

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        service_type: "contract_review"
    - table: "matter_tasks"
      count: ">= 1"
    - table: "matter_evidence_list_items"
      count: ">= 1"

skills:
  executed:
    - skill_id: "contract-intake"
      status: "completed"
    - skill_id: "contract-analysis"
      status: "completed"
    - skill_id: "risk-identification"
      status: "completed"

trace:
  expectations:
    - span_name: "run_skill"
      count: ">= 3"
    - span_name: "llm_call"
      count: ">= 5"

phase_gates:
  checkpoints:
    - phase: "intake"
      status: "completed"
      required_outputs: ["profile.client", "profile.contract_type"]
    - phase: "analysis"
      status: "completed"
      required_outputs: ["risk_assessment", "recommendations"]

document:
  quality:
    format:
      not_applicable: true
    style:
      legal_terms_check: true
      formal_language: true
    content:
      must_include:
        - "违约金"
        - "5%"
        - "免责"
        - "仲裁"
        - "风险"
        - "建议"
      must_not_include:
        - "{{.*}}"
        - "TODO"
```
