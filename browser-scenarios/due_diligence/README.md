---
name: due_diligence
description: 尽职调查场景 - 目标公司投资尽调
service_type: due_diligence
url: http://localhost:5175
credentials:
  username: admin
  password: admin123456
steps_file: browser_steps.yaml
---

# 尽职调查 - 目标公司投资尽调

## 案情描述

投资方拟对北京创新科技有限公司进行股权投资，需要对目标公司进行全面的尽职调查。调查范围包括公司基本情况、股权结构、财务状况、经营风险、重大合同、法律纠纷和合规性问题。

**调查重点**：
1. 公司基本情况和股权结构
2. 财务状况和经营风险
3. 重大合同和法律纠纷
4. 合规性问题

## 证据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| 营业执照 | assets/business_license.txt | 目标公司营业执照信息 |
| 财务报表 | assets/financial_report.txt | 2023年度财务报表摘要 |
| 重大合同 | assets/major_contracts.txt | 重大合同清单及风险提示 |

## 测试步骤

> **注意**: 结构化测试步骤定义在 `browser_steps.yaml` 文件中，供 Chrome DevTools MCP 自动化执行。

详见 [browser_steps.yaml](./browser_steps.yaml)

## 预期产物

- `docs/verification.png` - 尽调结果验证截图
- `docs/due_diligence_report.docx` - 尽职调查报告

## 验收标准

- [ ] 会话创建成功
- [ ] 尽调文件上传成功
- [ ] AI 返回尽调分析意见
- [ ] 分析意见识别出财务风险
- [ ] 分析意见识别出应收账款风险
- [ ] 分析意见识别出偿债压力
- [ ] 分析意见识别出客户集中度风险
- [ ] 分析意见包含合规性评估
- [ ] 尽调报告生成成功
- [ ] 截图保存成功

## Quality Check Expectations

```yaml
memory:
  retrieval:
    - entity_key: "company:target:name"
      must_include: ["北京创新科技", "创新科技"]
    - entity_key: "company:target:legal_representative"
      must_include: ["张伟"]
    - entity_key: "company:target:registered_capital"
      must_include: ["5000万", "5000"]
    - entity_key: "financial:revenue"
      must_include: ["12000万", "1.2亿"]
    - entity_key: "financial:net_profit"
      must_include: ["1400万"]
    - entity_key: "risk:identified"
      must_include: ["应收账款", "偿债", "客户集中"]
  storage:
    - entity_key: "company:target:name"
      scope: case
      expected_value_contains: "北京创新科技"
    - entity_key: "due_diligence:scope"
      scope: case
      expected_value_contains: "财务"
    - entity_key: "risk:financial"
      scope: case
    - entity_key: "risk:operational"
      scope: case
    - entity_key: "risk:legal"
      scope: case

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["公司法", "尽职调查", "投资"]
    - query_type: "due_diligence_template"
      must_match_count: ">= 0"

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        service_type: "due_diligence"
    - table: "matter_tasks"
      count: ">= 1"
    - table: "matter_evidence_list_items"
      count: ">= 3"

skills:
  executed:
    - skill_id: "due-diligence-intake"
      status: "completed"
    - skill_id: "due-diligence-analysis"
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
      count: ">= 6"

phase_gates:
  checkpoints:
    - phase: "intake"
      status: "completed"
      required_outputs: ["profile.target_company", "profile.due_diligence_scope"]
    - phase: "analysis"
      status: "completed"
      required_outputs: ["risk_assessment", "compliance_review"]
    - phase: "document_generation"
      status: "completed"
      required_outputs: ["due_diligence_report"]

document:
  quality:
    format:
      file_type: "docx"
      not_empty: true
    style:
      legal_terms_check: true
      formal_language: true
    content:
      must_include:
        - "北京创新科技"
        - "张伟"
        - "5000万"
        - "财务"
        - "风险"
        - "应收账款"
        - "偿债"
        - "合规"
      must_not_include:
        - "{{.*}}"
        - "TODO"
        - "待补充"
```

## 测试路径

本场景提供两种测试路径：

### 1. 渐进式对话 (progressive.yaml)
- **R1**: 上传营业执照 + 简单描述 → clarify 卡片
- **R2**: resume_card 补充尽调范围 → 分析
- **R3**: chat 请求生成报告 → select 卡片
- **R4**: resume_card 选择报告类型 → confirm 卡片
- **R5**: resume_card 确认报告 → 完成

### 2. 一次性完整输入 (one_shot.yaml)
- **R1**: 上传全部材料 + 完整描述 → 分析
- **R2**: chat 请求生成报告 → select 卡片
- **R3**: resume_card 选择报告类型 → confirm 卡片
- **R4**: resume_card 确认报告 → 完成

## 关键验证点

1. **intake 阶段**：
   - 正确识别目标公司名称、法定代表人、注册资本
   - 正确理解尽调范围和重点

2. **analysis 阶段**：
   - 财务风险分析（应收账款、偿债压力、盈利能力）
   - 经营风险分析（客户集中度、营运能力）
   - 法律风险分析（重大合同、担保风险）
   - 合规性评估

3. **document_generation 阶段**：
   - 报告结构完整
   - 数据准确引用
   - 风险提示明确
   - 建议具体可行

## 注意事项

1. 本场景依赖真实的 AI 引擎和知识库，需要正确配置环境变量
2. 尽调分析可能需要较长时间，建议设置合理的超时时间
3. 报告生成依赖模板系统，需要确保模板已正确部署
4. 测试数据为虚构，仅用于测试目的
