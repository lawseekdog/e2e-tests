## E2E 质量检查报告

### 基本信息
- **场景**: contract_review
- **Session ID**: 1
- **Matter ID**: 1
- **User ID**: 1
- **Organization ID**: 1
- **检查时间**: 2026-02-01

### 检查结果摘要

| 检查项 | 状态 | 通过/总数 | 说明 |
|--------|------|-----------|------|
| 基础连接 | ✅ | 1/1 | Session 和 Matter 数据获取成功 |
| 记忆提取 | ⏸️ | 0/4 | 需要数据库连接验证 |
| 记忆存储 | ⏸️ | 0/5 | 需要数据库连接验证 |
| 知识库命中 | ⏸️ | 0/2 | 需要知识库 API 验证 |
| Matter 记录 | ⏸️ | 0/3 | 需要数据库连接验证 |
| 技能执行 | ⏸️ | 0/3 | 需要 Trace API 验证 |
| Trace 验证 | ⏸️ | 0/2 | 需要 Trace API 验证 |
| 阶段门控 | ⏸️ | 0/2 | 需要 Phase Timeline API 验证 |
| 文书质量 | ⏸️ | 0/6 | 需要文档下载和内容分析 |

**总体状态**: 基础验证通过，详细检查需要完整环境

### 场景预期配置

#### Memory 预期

**Retrieval (记忆提取)**:
- `party:client:primary`: 应包含 "甲方", "北京甲方科技"
- `party:counterparty:primary`: 应包含 "乙方", "上海乙方供应链"
- `amount:contract:total`: 应包含 "200000", "20万"
- `risk:identified`: 应包含 "违约金", "免责", "仲裁"

**Storage (记忆存储)**:
- `party:client:primary` (scope: case): 应包含 "甲方"
- `contract:type` (scope: case): 应包含 "采购"
- `risk:penalty_clause` (scope: case): 应存在
- `risk:disclaimer_clause` (scope: case): 应存在
- `risk:arbitration_clause` (scope: case): 应存在

#### Knowledge 预期

**Hits (知识库命中)**:
- `legal_basis`: 至少 1 条，包含 "合同法", "违约责任", "免责条款"
- `contract_template`: 至少 0 条

#### Matter 预期

**Records (数据记录)**:
- `matters` 表: 1 条记录，service_type = "contract_review"
- `matter_tasks` 表: 至少 1 条记录
- `matter_evidence_list_items` 表: 至少 1 条记录

#### Skills 预期

**Executed (已执行技能)**:
- `contract-intake`: status = "completed"
- `contract-analysis`: status = "completed"
- `risk-identification`: status = "completed"

#### Trace 预期

**Expectations (执行轨迹)**:
- `run_skill`: 至少 3 次
- `llm_call`: 至少 5 次

#### Phase Gates 预期

**Checkpoints (阶段检查点)**:
- `intake` 阶段: status = "completed", 输出 profile.client, profile.contract_type
- `analysis` 阶段: status = "completed", 输出 risk_assessment, recommendations

#### Document 预期

**Quality (文书质量)**:

格式检查: 不适用 (not_applicable: true)

风格检查:
- 法律术语规范: 是
- 语言正式度: 是

内容检查:
- 必须包含: "违约金", "5%", "免责", "仲裁", "风险", "建议"
- 禁止包含: 模板占位符 `{{.*}}`, "TODO"

### 已验证项

#### ✅ 基础连接验证

**Session 信息**:
```json
{
  "id": "1",
  "organization_id": "1",
  "user_id": 1,
  "matter_id": 1,
  "title": "新咨询",
  "status": "active",
  "engagement_mode": "start_service",
  "service_type_id": "contract_review",
  "client_role": "client",
  "message_count": 3,
  "created_at": "2026-01-31T21:07:22.592877Z",
  "updated_at": "2026-01-31T21:33:49.072256Z"
}
```

**验证结果**:
- ✅ Session 存在且状态为 active
- ✅ Service Type 正确: contract_review
- ✅ Matter ID 已关联: 1
- ✅ 消息数量: 3 条

### 待验证项

以下检查项需要完整的测试环境（数据库连接、API 访问权限）才能执行：

#### ⏸️ 记忆提取检查
需要访问 Memory Service API 查询 entity_key 和内容

#### ⏸️ 记忆存储检查
需要访问 Memory Service 数据库查询 memory_facts 表

#### ⏸️ 知识库命中检查
需要访问 Knowledge Service API 执行搜索查询

#### ⏸️ Matter 记录检查
需要访问 Matter Service 数据库查询相关表

#### ⏸️ 技能执行检查
需要访问 Matter Service Trace API 查询执行历史

#### ⏸️ Trace 验证检查
需要访问 Trace API 统计 span 数量

#### ⏸️ 阶段门控检查
需要访问 Phase Timeline API 查询阶段状态

#### ⏸️ 文书质量检查
需要下载生成的文档并进行内容分析

### 建议

1. **环境配置**: 确保以下环境变量已正确配置
   - `E2E_PG_HOST`: PostgreSQL 主机地址
   - `E2E_PG_PORT`: PostgreSQL 端口
   - `E2E_PG_USER`: PostgreSQL 用户名
   - `E2E_PG_PASSWORD`: PostgreSQL 密码
   - `E2E_MATTER_DB`: Matter Service 数据库名
   - `E2E_MEMORY_DB`: Memory Service 数据库名
   - `INTERNAL_API_KEY`: 内部 API 密钥

2. **数据库访问**: 确保测试环境可以访问各微服务的数据库

3. **API 权限**: 确保测试账号有权限访问内部 API 端点

4. **完整测试**: 运行完整的 E2E 测试流程后再执行质量检查
   ```bash
   pytest tests/lawyer_workbench/contract_review/test_flow.py -v
   ```

5. **使用完整版脚本**: 在环境配置完成后，使用 `e2e_quality_check.py` 执行完整检查
   ```bash
   python scripts/e2e_quality_check.py contract_review 1
   ```

### 总结

本次质量检查成功验证了基础连接和 Session/Matter 数据的正确性。场景配置已正确加载，预期定义清晰完整。

要执行完整的质量检查，需要：
1. 配置数据库连接参数
2. 确保所有微服务正常运行
3. 使用具有完整权限的测试账号

当前验证结果表明系统基础功能正常，Session 和 Matter 数据结构符合预期。
