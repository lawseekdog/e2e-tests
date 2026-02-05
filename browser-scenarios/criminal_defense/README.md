# 刑事辩护场景测试

## 场景描述

本场景模拟刑事案件辩护流程，测试系统在刑事辩护场景下的完整对话流程和文书生成能力。

## 案件信息

- **被告人**：王某E2E01
- **案由**：盗窃罪
- **案情**：2023年6月15日凌晨，被告人在某小区内盗窃电动车一辆，价值人民币3000元
- **辩护方向**：初犯、认罪态度好、积极退赃、取得被害人谅解
- **预期产物**：辩护意见书（defense_opinion.docx）

## 测试路径

### 1. progressive（渐进式对话）- 6轮交互

适用于信息不完整的场景，需要逐步补充信息。

**交互流程**：
1. R1: 初始案情输入 + 上传起诉书 → clarify 卡片（补充被告人信息）
2. R2: 补充被告人身份信息 → select 卡片（罪名确认）
3. R3: 确认罪名 → select 卡片（辩护策略选择）
4. R4: 选择辩护策略 → select 卡片（文书选择）
5. R5: 选择文书类型 → confirm 卡片（文书审核）
6. R6: 确认文书 → 完成，生成辩护意见书

### 2. one_shot（一次性完整输入）- 5轮交互

适用于信息完整的场景，跳过 clarify 环节。

**交互流程**：
1. R1: 完整案情输入（含被告人详细信息）+ 上传证据 → select 卡片（罪名确认）
2. R2: 确认罪名 → select 卡片（辩护策略选择）
3. R3: 选择辩护策略 → select 卡片（文书选择）
4. R4: 选择文书类型 → confirm 卡片（文书审核）
5. R5: 确认文书 → 完成，生成辩护意见书

### 3. rollback（回退补充场景）- 8轮交互

适用于需要补充材料的场景，在文书确认环节发现问题触发回退。

**交互流程**：
1. R1-R4: 同 progressive 前4轮
2. R5: 选择文书类型 → confirm 卡片（文书审核）
3. R6: 发现问题，拒绝确认 → clarify 卡片（补充辩护理由）
4. R7: 补充家庭情况和社会危险性评估 + 上传补充材料 → confirm 卡片（重新审核）
5. R8: 确认文书 → 完成，生成辩护意见书

## 测试数据

### 证据材料

- `assets/indictment.txt` - 起诉书（检察院提起公诉的法律文书）
- `assets/case_materials.txt` - 案件材料（包含讯问笔录、被害人陈述、证人证言等）
- `assets/evidence_list.txt` - 证据清单（23项证据材料的详细清单）
- `assets/additional_materials.txt` - 补充材料（家庭情况、社会危险性评估、谅解书等）

## 涉及的 Skill

- `criminal-defense-intake` - 刑事辩护案件接入
- `charge-analysis` - 罪名分析与辩护策略
- `documents` - 文书类型选择
- `document-generation` - 文书生成

## 预期产物

- **文书类型**：辩护意见书（defense_opinion）
- **文件格式**：DOCX
- **主要内容**：
  - 案件基本信息
  - 辩护意见（初犯、认罪态度好、积极退赃、取得谅解）
  - 量刑建议（建议适用缓刑）
  - 法律依据

## 运行测试

```bash
pytest tests/test_browser_scenarios.py::test_criminal_defense_progressive -v
pytest tests/test_browser_scenarios.py::test_criminal_defense_one_shot -v
pytest tests/test_browser_scenarios.py::test_criminal_defense_rollback -v
```

## 验证要点

1. **对话流程验证**：
   - 各轮对话按预期流程进行
   - SSE 事件序列正确（skill_start → delta → tool_start → tool_end → card → end）
   - 卡片类型正确（clarify / select / confirm）

2. **文书生成验证**：
   - 文书成功生成并可下载
   - 文书内容包含关键信息（被告人姓名、罪名、辩护意见等）
   - 文书格式符合规范

3. **回退机制验证**（rollback 路径）：
   - 拒绝确认文书后触发 clarify 卡片
   - 补充材料后重新生成文书
   - 最终文书包含补充的内容

## 注意事项

1. 本测试依赖真实环境，需要配置正确的 `BASE_URL`、`INTERNAL_API_KEY` 和 `LLM_KEY`
2. 测试数据中的人名、地名均为虚构，仅用于测试目的
3. 测试超时时间设置为 180 秒，确保 LLM 有足够时间生成文书
