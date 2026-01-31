---
name: arbitration_commercial
description: 商事仲裁场景 - 买卖合同纠纷（申请人）
service_type: arbitration
url: http://localhost:5175
credentials:
  username: admin
  password: admin123456
steps_file: browser_steps.yaml
---

# 商事仲裁 - 买卖合同纠纷

## 案情描述

申请人甲方公司E2E01与被申请人乙方公司E2E01于2023年3月1日签订《设备采购合同》，约定被申请人向申请人采购智能生产线设备一套，总价款500万元，交货期为2023年6月30日前，付款方式为货到验收合格后30日内付清全款。

申请人已于2023年6月20日按约交付全部设备，并于2023年6月25日经被申请人验收合格。被申请人于2023年7月15日支付首笔货款200万元，但剩余300万元至今未付。申请人多次催收无果。

根据合同第八条约定，本合同争议提交北京仲裁委员会仲裁解决。申请人为维护自身合法权益，特向北京仲裁委员会提起仲裁。

**仲裁请求**：
1. 裁决被申请人支付剩余货款300万元
2. 裁决被申请人按合同约定支付逾期付款违约金（按日万分之五计算）
3. 本案仲裁费用由被申请人承担

## 证据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 采购合同 | assets/contract.txt | 设备采购合同原件 |
| 违约证据 | assets/breach_evidence.txt | 验收单、付款记录、催款函 |
| 仲裁条款 | assets/arbitration_clause.txt | 合同仲裁条款详细说明 |
| 补充证据 | assets/additional_evidence.txt | 银行转账凭证、邮件往来、通话记录 |

## 测试路径

本场景包含三种对话路径，用于测试不同的交互模式：

### 1. 渐进式对话 (progressive.yaml)
- **轮次**: 6轮
- **特点**: 逐步补充信息，适用于信息不完整的场景
- **流程**: 初始输入 → 补充仲裁条款 → 确认案由 → 选择仲裁请求 → 选择文书 → 确认文书

### 2. 一次性完整输入 (one_shot.yaml)
- **轮次**: 5轮
- **特点**: 一次性提供完整信息，跳过澄清环节
- **流程**: 完整输入 → 确认案由 → 选择仲裁请求 → 选择文书 → 确认文书

### 3. 回退补充证据 (rollback.yaml)
- **轮次**: 8轮
- **特点**: 文书确认时发现证据不足，触发回退补充
- **流程**: 初始输入 → 补充信息 → 确认案由 → 选择请求 → 选择文书 → 发现问题 → 补充证据 → 确认文书

## 测试步骤

> **注意**: 结构化测试步骤定义在 `browser_steps.yaml` 文件中，供 Chrome DevTools MCP 自动化执行。

详见 [browser_steps.yaml](./browser_steps.yaml)

## 预期产物

- `docs/verification.png` - 流程验证截图
- `docs/arbitration_application.docx` - 生成的仲裁申请书

## 验收标准

- [ ] 会话创建成功
- [ ] 证据文件上传成功
- [ ] AI 返回案情分析
- [ ] 仲裁申请书 DOCX 生成成功
- [ ] 仲裁申请书内容包含当事人信息
- [ ] 仲裁申请书内容包含货款金额
- [ ] 仲裁申请书内容包含仲裁条款
- [ ] 截图和文档保存成功

## Quality Check Expectations

```yaml
memory:
  retrieval:
    - entity_key: "party:applicant:primary"
      must_include: ["甲方公司E2E01"]
    - entity_key: "party:respondent:primary"
      must_include: ["乙方公司E2E01"]
    - entity_key: "amount:claim:principal"
      must_include: ["3000000", "300万"]
    - entity_key: "date:contract"
      must_include: ["2023年3月1日"]
    - entity_key: "date:delivery"
      must_include: ["2023年6月20日"]
    - entity_key: "date:acceptance"
      must_include: ["2023年6月25日"]
  storage:
    - entity_key: "party:applicant:primary"
      scope: case
      expected_value_contains: "甲方公司E2E01"
    - entity_key: "party:respondent:primary"
      scope: case
      expected_value_contains: "乙方公司E2E01"
    - entity_key: "cause_of_action"
      scope: case
      expected_value_contains: "买卖合同"
    - entity_key: "amount:claim:principal"
      scope: case

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["买卖合同", "民法典", "仲裁法"]
    - query_type: "arbitration_rules"
      must_match_count: ">= 1"
      must_include_keywords: ["北京仲裁委员会", "仲裁规则"]

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        cause_of_action_code: "commercial_contract_dispute"
    - table: "matter_deliverables"
      output_key: "arbitration_application"
      count: 1
    - table: "matter_evidence_list_items"
      count: ">= 4"
    - table: "matter_parties"
      count: 2
      conditions:
        roles: ["applicant", "respondent"]

skills:
  executed:
    - skill_id: "arbitration-intake"
      status: "completed"
    - skill_id: "cause-recommendation"
      status: "completed"
    - skill_id: "documents"
      status: "completed"
    - skill_id: "document-generation"
      status: "completed"

trace:
  expectations:
    - span_name: "run_skill"
      count: ">= 4"
    - span_name: "llm_call"
      count: ">= 8"
    - span_name: "tool_call"
      count: ">= 3"

phase_gates:
  checkpoints:
    - phase: "intake"
      status: "completed"
      required_outputs: ["profile.applicant", "profile.respondent", "profile.facts"]
    - phase: "analysis"
      status: "completed"
      required_outputs: ["cause_of_action", "arbitration_requests"]
    - phase: "document"
      status: "completed"
      required_outputs: ["arbitration_application"]

document:
  quality:
    format:
      title_centered: true
      signature_right_aligned: true
      page_margins: "standard"
    style:
      legal_terms_check: true
      formal_language: true
      no_colloquial_expressions: true
    content:
      must_include:
        - "申请人"
        - "被申请人"
        - "仲裁请求"
        - "事实与理由"
        - "甲方公司E2E01"
        - "乙方公司E2E01"
        - "3000000"
        - "买卖合同"
        - "北京仲裁委员会"
      must_not_include:
        - "{{.*}}"
        - "TODO"
        - "PLACEHOLDER"
        - "undefined"
```
