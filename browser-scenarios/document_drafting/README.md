# Document Drafting Scenario - 文书代写场景

## 场景概述

本场景测试 LawSeekDog 系统的文书代写能力，模拟用户通过对话方式生成劳动仲裁申请书的完整流程。

### 业务场景
- **场景类型**：文书代写
- **具体案例**：劳动争议 - 劳动仲裁申请书
- **涉及技能**：
  - `document-drafting-intake`：文书代写信息收集
  - `documents`：文书类型选择
  - `document-generation`：文书生成

### 测试路径

#### 1. Progressive Path（渐进式路径）
4 轮交互，逐步收集信息：
- **Round 1**：用户描述劳动争议 → AI 返回 clarify 卡片（收集当事人信息）
- **Round 2**：用户补充当事人信息 → AI 返回 select 卡片（文书类型选择）
- **Round 3**：用户选择文书类型 → AI 返回 confirm 卡片（文书预览）
- **Round 4**：用户确认生成 → 完成文书生成

#### 2. One-Shot Path（一次性路径）
3 轮交互，一次性提供完整信息：
- **Round 1**：用户上传材料 + 完整描述 → AI 返回 select 卡片
- **Round 2**：用户选择文书类型 → AI 返回 confirm 卡片
- **Round 3**：用户确认生成 → 完成文书生成

## 测试数据

### Assets 文件
- `labor_contract.txt`：劳动合同（包含入职时间、岗位、工资等信息）
- `salary_record.txt`：工资流水（证明工资标准和拖欠情况）
- `dismissal_notice.txt`：解除劳动合同通知书（证明辞退事实）

### 测试用例数据
- **当事人**：李四（申请人）、上海信息技术有限公司（被申请人）
- **劳动关系**：产品经理，2021年3月15日入职，2024年11月30日被辞退
- **争议焦点**：违法解除劳动合同、拖欠工资、经济补偿金
- **诉求金额**：拖欠工资 36,000 元（2个月）+ 经济补偿金

## Quality Check Expectations

### 1. 技能调用验证
- ✅ 正确识别文书代写场景
- ✅ 按顺序调用 `document-drafting-intake` → `documents` → `document-generation`
- ✅ 每个技能返回正确的卡片类型（clarify/select/confirm）

### 2. 信息提取与保留
- ✅ 准确提取当事人信息（姓名、公司、职位、入职时间、辞退时间、工资）
- ✅ 上下文信息在多轮对话中正确保留
- ✅ 从上传文件中提取关键信息（合同条款、工资记录、辞退理由）

### 3. 文书生成质量
- ✅ 文书结构完整（标题、申请人信息、被申请人信息、仲裁请求、事实与理由）
- ✅ 仲裁请求准确（拖欠工资金额计算正确、包含经济补偿金）
- ✅ 事实与理由逻辑清晰（入职时间、辞退时间、辞退理由、违法性分析）
- ✅ 法律依据引用正确（《劳动合同法》相关条款）

### 4. 卡片交互验证
- ✅ Clarify 卡片包含必要字段（name, company, position, employment_date）
- ✅ Select 卡片提供合理的文书类型选项（劳动仲裁申请书、劳动争议调解申请书等）
- ✅ Confirm 卡片展示完整的文书预览内容
- ✅ Resume 按钮功能正常，可恢复对话

### 5. 文件处理验证
- ✅ 文件上传成功（labor_contract.txt）
- ✅ 文件内容正确解析
- ✅ 生成的文书文件格式正确（.docx）
- ✅ 文书文件可下载且内容完整

### 6. 事项管理验证
- ✅ 创建文书代写事项（matter）
- ✅ 事项状态正确更新（pending → in_progress → completed）
- ✅ 事项产物正确关联（labor_arbitration_application.docx）
- ✅ 会话历史完整记录

### 7. 边界情况处理
- ✅ 信息不完整时正确引导用户补充
- ✅ 文书类型选择错误时可重新选择
- ✅ 金额计算准确（工资 × 月数）
- ✅ 时间格式统一（YYYY年MM月DD日）

## 预期输出

### 生成文件
- `docs/verification.png`：最终验证截图
- `docs/labor_arbitration_application.docx`：生成的劳动仲裁申请书

### 文书内容要求
```
劳动仲裁申请书

申请人：李四
被申请人：上海信息技术有限公司

仲裁请求：
1. 请求裁决被申请人支付拖欠工资 36,000 元
2. 请求裁决被申请人支付违法解除劳动合同的经济补偿金
3. 本案仲裁费用由被申请人承担

事实与理由：
申请人于 2021年3月15日 入职被申请人公司，担任产品经理职位...
（详细事实描述）

法律依据：
《中华人民共和国劳动合同法》第四十七条、第八十七条...

此致
XX市劳动人事争议仲裁委员会

申请人：李四
日期：YYYY年MM月DD日
```

## 运行测试

```bash
pytest tests/browser/test_document_drafting.py -v
pytest tests/browser/test_document_drafting.py::test_progressive_path -v
pytest tests/browser/test_document_drafting.py::test_one_shot_path -v
```

## 注意事项

1. **LLM 依赖**：本测试依赖真实 LLM 服务，需要配置正确的 API Key
2. **超时设置**：文书生成可能需要较长时间，已设置 180 秒超时
3. **文件路径**：确保 assets 目录下的测试文件存在且可读
4. **环境变量**：需要设置 `BASE_URL`、`INTERNAL_API_KEY`、`LLM_KEY`
5. **浏览器驱动**：需要安装 Chrome DevTools Protocol 支持

## 故障排查

### 常见问题
1. **技能未调用**：检查技能配置和触发条件
2. **信息提取失败**：检查 LLM 提示词和响应格式
3. **文书生成失败**：检查文书模板和数据绑定
4. **文件上传失败**：检查文件路径和权限
5. **卡片未显示**：检查前端卡片渲染逻辑

### 调试建议
- 查看浏览器控制台日志
- 检查网络请求和响应
- 查看后端服务日志
- 使用 `screenshot_on_failure` 保存失败时的截图
