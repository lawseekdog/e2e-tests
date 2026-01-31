---
name: civil_prosecution
description: 民事起诉场景 - 民间借贷纠纷（原告）
service_type: civil_first_instance
url: http://localhost:5175
credentials:
  username: admin
  password: admin123456
steps_file: steps.yaml
---

# 民事起诉 - 民间借贷纠纷

## 案情描述

原告张三E2E01与被告李四E2E01系朋友关系。2023年1月1日，被告因个人资金周转需要向原告借款人民币100,000元，约定于2023年12月31日前归还。原告通过银行转账方式向被告交付了借款，被告出具了借条。

借款到期后，被告未按约定归还借款。原告多次催收，被告均以各种理由推脱。原告为维护自身合法权益，诉至法院。

**原告诉求**：
1. 判令被告归还借款本金100,000元
2. 判令被告按年利率6%支付逾期利息
3. 本案诉讼费由被告承担

## 证据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 借条 | assets/iou.txt | 被告出具的借条原件 |
| 转账记录 | assets/sample_transfer_record.txt | 银行转账凭证 |
| 聊天记录 | assets/sample_chat_record.txt | 催收聊天记录 |

## 测试步骤

> **注意**: 结构化测试步骤定义在 `steps.yaml` 文件中，供 Chrome DevTools MCP 自动化执行。

详见 [steps.yaml](./steps.yaml)

## 预期产物

- `docs/verification.png` - 流程验证截图
- `docs/civil_complaint.docx` - 生成的民事起诉状

## 验收标准

- [ ] 会话创建成功
- [ ] 证据文件上传成功
- [ ] AI 返回案情分析
- [ ] 起诉状 DOCX 生成成功
- [ ] 起诉状内容包含当事人信息
- [ ] 起诉状内容包含借款金额
- [ ] 截图和文档保存成功

## Quality Check Expectations

```yaml
memory:
  retrieval:
    - entity_key: "party:plaintiff:primary"
      must_include: ["张三E2E01"]
    - entity_key: "party:defendant:primary"
      must_include: ["李四E2E01"]
    - entity_key: "amount:claim:principal"
      must_include: ["100000", "10万"]
    - entity_key: "date:loan"
      must_include: ["2023年1月1日"]
    - entity_key: "date:due"
      must_include: ["2023年12月31日"]
  storage:
    - entity_key: "party:plaintiff:primary"
      scope: case
      expected_value_contains: "张三E2E01"
    - entity_key: "party:defendant:primary"
      scope: case
      expected_value_contains: "李四E2E01"
    - entity_key: "cause_of_action"
      scope: case
      expected_value_contains: "民间借贷"
    - entity_key: "amount:claim:principal"
      scope: case

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["民间借贷", "合同法", "借款合同"]
    - query_type: "case_reference"
      must_match_count: ">= 0"

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        cause_of_action_code: "civil_loan"
    - table: "matter_deliverables"
      output_key: "civil_complaint"
      count: 1
    - table: "matter_evidence_list_items"
      count: ">= 3"
    - table: "matter_parties"
      count: 2
      conditions:
        roles: ["plaintiff", "defendant"]

skills:
  executed:
    - skill_id: "litigation-intake"
      status: "completed"
    - skill_id: "evidence-analysis"
      status: "completed"
    - skill_id: "issue-identification"
      status: "completed"
    - skill_id: "strategy-formulation"
      status: "completed"
    - skill_id: "document-generation"
      status: "completed"

trace:
  expectations:
    - span_name: "run_skill"
      count: ">= 5"
    - span_name: "llm_call"
      count: ">= 10"
    - span_name: "tool_call"
      count: ">= 3"

phase_gates:
  checkpoints:
    - phase: "intake"
      status: "completed"
      required_outputs: ["profile.plaintiff", "profile.defendant", "profile.facts"]
    - phase: "analysis"
      status: "completed"
      required_outputs: ["evidence_list", "issues", "strategies"]
    - phase: "document"
      status: "completed"
      required_outputs: ["civil_complaint"]

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
        - "原告"
        - "被告"
        - "诉讼请求"
        - "事实与理由"
        - "张三E2E01"
        - "李四E2E01"
        - "100000"
        - "民间借贷"
      must_not_include:
        - "{{.*}}"
        - "TODO"
        - "PLACEHOLDER"
        - "undefined"
```
