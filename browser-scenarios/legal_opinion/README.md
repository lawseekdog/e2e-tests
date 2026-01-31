# Legal Opinion Scenario - 法律意见书场景

## 场景描述

本场景测试法律意见书生成功能，模拟律师为股权转让交易出具法律意见书的完整流程。

### 业务背景
- **交易类型**：股权转让
- **转让方**：张三（持有北京XX科技有限公司30%股权）
- **受让方**：李四
- **转让价款**：人民币600万元
- **意见书主题**：股权转让交易的合法性与风险分析

## 测试路径

### 1. Progressive Path（渐进式）
**文件**：`paths/progressive.yaml`

**交互流程**（5轮）：
1. **R1**: 上传股权转让协议 + 简单描述 → AI返回clarify卡片要求补充意见书主题
2. **R2**: resume_card补充意见书主题 → AI进行法律分析
3. **R3**: chat请求生成意见书 → AI返回select卡片选择意见书类型
4. **R4**: resume_card选择"股权转让法律意见书" → AI返回confirm卡片确认生成
5. **R5**: resume_card确认生成 → AI生成意见书并提供下载

**涉及Skills**：
- `legal-opinion-intake` - 意见书需求收集
- `legal-opinion-analysis` - 法律分析
- `documents` - 文书选择
- `document-generation` - 文书生成

**卡片交互**：
- Round 1: `clarify` 卡片（补充意见书主题）
- Round 3: `select` 卡片（选择意见书类型）
- Round 4: `confirm` 卡片（确认生成意见书）

### 2. One-Shot Path（一次性）
**文件**：`paths/one_shot.yaml`

**交互流程**（3轮）：
1. **R1**: 上传所有材料（协议+公司信息+股东会决议）+ 完整描述 → AI返回select卡片
2. **R2**: resume_card选择意见书类型 → AI返回confirm卡片
3. **R3**: resume_card确认生成 → AI生成意见书并提供下载

**涉及Skills**：
- `legal-opinion-intake` - 意见书需求收集
- `document-generation` - 文书生成

**卡片交互**：
- Round 1: `select` 卡片（选择意见书类型）
- Round 2: `confirm` 卡片（确认生成意见书）

## 测试资产

### 文件清单
```
assets/
├── equity_transfer_agreement.txt  # 股权转让协议
├── company_info.txt               # 公司基本信息
└── shareholder_resolution.txt     # 股东会决议
```

### 文件说明
1. **equity_transfer_agreement.txt**
   - 股权转让协议正文
   - 包含：转让方、受让方、转让股权比例、价款、交割条件、陈述保证、违约责任等

2. **company_info.txt**
   - 目标公司基本信息
   - 包含：公司名称、注册资本、股权结构、财务数据、合规情况等

3. **shareholder_resolution.txt**
   - 股东会决议
   - 包含：同意股权转让、其他股东放弃优先购买权、授权办理变更登记等

## Quality Check Expectations

### 1. Skill调用验证
- ✅ 正确调用 `legal-opinion-intake` skill
- ✅ 正确调用 `legal-opinion-analysis` skill（仅progressive路径）
- ✅ 正确调用 `documents` skill（仅progressive路径）
- ✅ 正确调用 `document-generation` skill

### 2. 卡片交互验证
- ✅ clarify卡片正确展示并可填写（progressive路径）
- ✅ select卡片正确展示意见书类型选项
- ✅ confirm卡片正确展示待生成文书信息
- ✅ 卡片交互后AI正确继续流程

### 3. 文书生成验证
- ✅ 生成的意见书为.docx格式
- ✅ 意见书包含必要章节：
  - 基本情况
  - 法律分析
  - 风险提示
  - 法律意见
- ✅ 意见书内容与上传材料相关
- ✅ 意见书可正常下载

### 4. 内容质量验证（one-shot路径）
- ✅ 包含交易主体资格分析
- ✅ 包含股权转让程序合规性分析
- ✅ 包含法律风险提示
- ✅ 分析基于上传的三份材料

### 5. 用户体验验证
- ✅ 文件上传成功且有反馈
- ✅ AI响应时间在合理范围内（<90秒）
- ✅ 错误情况有明确提示
- ✅ 最终生成的文书可访问

## 运行方式

```bash
# 运行所有路径
pytest tests/test_browser_scenarios.py::test_legal_opinion -v

# 运行特定路径
pytest tests/test_browser_scenarios.py::test_legal_opinion[progressive] -v
pytest tests/test_browser_scenarios.py::test_legal_opinion[one_shot] -v
```

## 预期输出

```
docs/
├── verification.png          # 最终验证截图
└── legal_opinion.docx        # 生成的法律意见书
```

## 注意事项

1. **环境要求**
   - 需要真实的LLM环境（不mock）
   - 需要配置正确的API密钥
   - 需要文书生成服务正常运行

2. **超时设置**
   - 文书生成可能需要较长时间，已设置90秒超时
   - 如遇超时可适当调整timeout参数

3. **数据隐私**
   - 测试数据为虚构，不涉及真实个人或公司信息
   - 生成的文书仅用于测试验证

4. **失败诊断**
   - 失败时会自动截图保存到docs/目录
   - 检查AI响应是否包含预期的关键词
   - 检查skill调用链是否完整
