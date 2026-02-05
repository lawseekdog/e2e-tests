---
name: labor_arbitration_applicant
description: 劳动仲裁场景 - 劳动争议仲裁申请
service_type: labor_arbitration
url: http://localhost:5175
credentials:
  username: admin
  password: admin123456
steps_file: browser_steps.yaml
---

# 劳动仲裁 - 劳动争议

## 案情描述

申请人王五E2E01于2023年1月1日入职被申请人某科技公司E2E01，担任软件工程师，月薪15,000元。双方签订了固定期限劳动合同，合同期限至2025年12月31日。

2024年6月起，被申请人开始拖欠申请人工资，累计拖欠3个月（2024年6月、7月、8月），共计45,000元。申请人多次向公司财务部门催讨，均未得到回复。

2024年9月1日，被申请人以"客观情况发生重大变化"为由违法解除劳动合同，未支付经济补偿金。申请人为维护自身合法权益，向劳动争议仲裁委员会申请仲裁。

**仲裁请求**：
1. 裁决被申请人支付拖欠工资45,000元
2. 裁决被申请人支付违法解除劳动合同经济补偿金30,000元
3. 本案仲裁费由被申请人承担

## 证据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 劳动合同 | assets/labor_contract.txt | 双方签订的劳动合同 |
| 工资流水 | assets/salary_record.txt | 银行工资流水记录 |
| 解除通知 | assets/dismissal_notice.txt | 公司出具的解除劳动合同通知书 |
| 补充证据 | assets/additional_evidence.txt | 补充证据说明材料 |

## 对话路径

本场景包含三种对话路径：

### 1. 渐进式对话 (progressive.yaml)
逐步补充信息，适用于信息不完整的场景。共7轮交互：
- R1: 初始案情输入 → clarify 卡片（补充当事人信息）
- R2: 补充当事人信息 → clarify 卡片（补充工资拖欠信息）
- R3: 补充工资拖欠信息 → select 卡片（案由确认）
- R4: 确认案由 → select 卡片（仲裁请求选择）
- R5: 选择仲裁请求 → select 卡片（文书选择）
- R6: 选择文书类型 → confirm 卡片（文书审核）
- R7: 确认文书 → 完成

### 2. 一次性完整输入 (one_shot.yaml)
用户一次性提供完整信息，跳过 clarify 阶段。共5轮交互：
- R1: 完整案情输入 → select 卡片（案由确认）
- R2: 确认案由 → select 卡片（仲裁请求选择）
- R3: 选择仲裁请求 → select 卡片（文书选择）
- R4: 选择文书类型 → confirm 卡片（文书审核）
- R5: 确认文书 → 完成

### 3. 回退补充证据 (rollback.yaml)
在文书确认阶段发现证据不足，触发回退补充证据流程。共9轮交互：
- R1-R6: 同 progressive
- R7: 发现证据不足 → clarify 卡片（补充证据）
- R8: 补充工资流水证据 → confirm 卡片（重新审核）
- R9: 确认文书 → 完成

## 测试步骤

> **注意**: 结构化测试步骤定义在 `browser_steps.yaml` 文件中，供 Chrome DevTools MCP 自动化执行。

详见 [browser_steps.yaml](./browser_steps.yaml)

## 预期产物

- `docs/verification.png` - 流程验证截图
- `docs/labor_arbitration_application.docx` - 生成的劳动仲裁申请书

## 验收标准

- [ ] 会话创建成功
- [ ] 证据文件上传成功
- [ ] AI 返回案情分析
- [ ] 劳动仲裁申请书 DOCX 生成成功
- [ ] 申请书内容包含当事人信息
- [ ] 申请书内容包含工资拖欠金额
- [ ] 申请书内容包含经济补偿金金额
- [ ] 截图和文档保存成功

## Quality Check Expectations

```yaml
memory:
  retrieval:
    - entity_key: "party:applicant:primary"
      must_include: ["王五E2E01"]
    - entity_key: "party:respondent:primary"
      must_include: ["某科技公司E2E01"]
    - entity_key: "amount:salary_arrears"
      must_include: ["45000", "4.5万"]
    - entity_key: "amount:economic_compensation"
      must_include: ["30000", "3万"]
    - entity_key: "date:employment_start"
      must_include: ["2023年1月1日"]
    - entity_key: "date:dismissal"
      must_include: ["2024年9月1日"]
  storage:
    - entity_key: "party:applicant:primary"
      scope: case
      expected_value_contains: "王五E2E01"
    - entity_key: "party:respondent:primary"
      scope: case
      expected_value_contains: "某科技公司E2E01"
    - entity_key: "cause_of_action"
      scope: case
      expected_value_contains: "劳动争议"
    - entity_key: "amount:salary_arrears"
      scope: case

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["劳动合同法", "劳动争议", "经济补偿"]
    - query_type: "case_reference"
      must_match_count: ">= 0"

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        cause_of_action_code: "labor_dispute"
    - table: "matter_deliverables"
      output_key: "labor_arbitration_application"
      count: 1
    - table: "matter_evidence_list_items"
      count: ">= 3"
    - table: "matter_parties"
      count: 2
      conditions:
        roles: ["applicant", "respondent"]

skills:
  executed:
    - skill_id: "labor-arbitration-intake"
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
      required_outputs: ["evidence_list", "arbitration_requests"]
    - phase: "document"
      status: "completed"
      required_outputs: ["labor_arbitration_application"]

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
        - "王五E2E01"
        - "某科技公司E2E01"
        - "45000"
        - "30000"
        - "劳动争议"
      must_not_include:
        - "{{.*}}"
        - "TODO"
        - "PLACEHOLDER"
        - "undefined"
```
