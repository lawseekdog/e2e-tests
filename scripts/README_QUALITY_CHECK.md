# E2E 质量检查工具

本目录包含 E2E 测试质量检查工具，用于验证测试完成后的系统状态是否符合预期。

## 工具列表

### 1. `e2e_quality_check.py` - 完整质量检查

完整的质量检查工具，执行所有 8 项检查。

**功能**:
- ✅ 记忆提取检查 (Memory Retrieval)
- ✅ 记忆存储检查 (Memory Storage)
- ✅ 知识库命中检查 (Knowledge Hits)
- ✅ Matter 记录检查 (Matter Records)
- ✅ 技能执行检查 (Skills Executed)
- ✅ Trace 验证检查 (Trace Expectations)
- ✅ 阶段门控检查 (Phase Gates)
- ✅ 文书质量检查 (Document Quality)

**依赖**:
- 数据库连接（PostgreSQL）
- 内部 API 访问权限
- 完整的测试环境

**用法**:
```bash
python scripts/e2e_quality_check.py <scenario_name> <session_id>
```

**示例**:
```bash
python scripts/e2e_quality_check.py contract_review 1
```

### 2. `e2e_quality_check_simple.py` - 简化质量检查

简化版本，仅验证基础连接和配置加载。

**功能**:
- ✅ 加载场景预期配置
- ✅ 验证 Session 存在
- ✅ 验证 Matter 关联
- ✅ 显示完整的预期配置

**依赖**:
- API 访问（无需数据库）
- 基础测试环境

**用法**:
```bash
python scripts/e2e_quality_check_simple.py <scenario_name> <session_id>
```

**示例**:
```bash
python scripts/e2e_quality_check_simple.py contract_review 1
```

## 环境配置

### 必需环境变量

```bash
# API 连接
E2E_BASE_URL=http://localhost:18001/api/v1
E2E_USERNAME=admin
E2E_PASSWORD=admin123456

# 数据库连接（完整检查需要）
E2E_PG_HOST=localhost
E2E_PG_PORT=5434
E2E_PG_USER=postgres
E2E_PG_PASSWORD=postgres
E2E_MATTER_DB=matter-service
E2E_MEMORY_DB=memory-service

# 内部 API 密钥（完整检查需要）
INTERNAL_API_KEY=your-internal-api-key
```

### 配置文件

可以在 `e2e-tests/.env` 文件中配置环境变量：

```bash
cp .env.example .env
# 编辑 .env 文件
```

## 场景定义

每个测试场景在 `e2e-tests/browser-scenarios/<scenario_name>/README.md` 中定义质量检查预期。

### 预期配置格式

```yaml
memory:
  retrieval:
    - entity_key: "party:client:primary"
      must_include: ["甲方", "北京甲方科技"]
  storage:
    - entity_key: "party:client:primary"
      scope: case
      expected_value_contains: "甲方"

knowledge:
  hits:
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["合同法", "违约责任"]

matter:
  records:
    - table: "matters"
      count: 1
      conditions:
        service_type: "contract_review"

skills:
  executed:
    - skill_id: "contract-intake"
      status: "completed"

trace:
  expectations:
    - span_name: "run_skill"
      count: ">= 3"

phase_gates:
  checkpoints:
    - phase: "intake"
      status: "completed"
      required_outputs: ["profile.client"]

document:
  quality:
    format:
      not_applicable: true
    content:
      must_include: ["违约金", "风险"]
      must_not_include: ["{{.*}}", "TODO"]
```

## 检查项说明

### 1. 记忆提取 (Memory Retrieval)

验证系统能够正确提取关键记忆：
- 查询 Memory Service API
- 验证 entity_key 存在
- 验证内容包含必需关键词

### 2. 记忆存储 (Memory Storage)

验证系统正确存储了关键事实：
- 查询 memory_facts 表
- 验证 entity_key 和 scope
- 验证存储值包含预期内容

### 3. 知识库命中 (Knowledge Hits)

验证知识库检索的精准度：
- 调用 Knowledge Service 搜索 API
- 验证结果数量
- 验证结果包含关键词

### 4. Matter 记录 (Matter Records)

验证事项数据正确落库：
- 查询 matters 表
- 查询 matter_tasks 表
- 查询 matter_evidence_list_items 表
- 验证记录数量和字段值

### 5. 技能执行 (Skills Executed)

验证预期的技能都已执行：
- 查询 Trace API
- 验证技能执行状态
- 记录未执行或失败的技能

### 6. Trace 验证 (Trace Expectations)

验证执行轨迹符合预期：
- 查询 Trace 数据
- 统计 span 数量
- 验证关键 span 存在

### 7. 阶段门控 (Phase Gates)

验证工作流阶段正确完成：
- 查询 Phase Timeline API
- 验证阶段状态
- 验证必需输出已生成

### 8. 文书质量 (Document Quality)

验证生成文书的质量：
- 下载文档
- 检查格式（标题、落款、页边距）
- 检查风格（法律术语、正式语言）
- 检查内容（必需内容、禁止内容）

## 输出报告

检查完成后会生成 Markdown 格式的报告，保存在 `e2e-tests/docs/` 目录：

```
docs/quality_check_<scenario_name>_<session_id>.md
```

报告包含：
- 基本信息
- 检查结果摘要
- 详细结果（通过项、失败项、警告）
- 文书质量评估
- 改进建议

## 使用流程

### 1. 运行 E2E 测试

```bash
pytest tests/lawyer_workbench/contract_review/test_flow.py -v
```

### 2. 获取 Session ID

从测试输出或日志中获取 session_id。

### 3. 执行质量检查

```bash
# 简化检查（快速验证）
python scripts/e2e_quality_check_simple.py contract_review <session_id>

# 完整检查（需要数据库）
python scripts/e2e_quality_check.py contract_review <session_id>
```

### 4. 查看报告

```bash
cat docs/quality_check_contract_review_<session_id>.md
```

## 故障排查

### 连接超时

**问题**: 登录或 API 调用超时

**解决**:
1. 检查服务是否运行
2. 检查 BASE_URL 配置
3. 检查网络连接

### 数据库连接失败

**问题**: 无法连接到 PostgreSQL

**解决**:
1. 检查数据库服务是否运行
2. 检查 PG_HOST、PG_PORT 配置
3. 检查用户名和密码
4. 检查数据库名称

### Session 不存在

**问题**: Session ID 无效

**解决**:
1. 确认 Session ID 正确
2. 检查 Session 是否已过期
3. 重新运行 E2E 测试生成新 Session

### 权限不足

**问题**: 无法访问内部 API

**解决**:
1. 配置 INTERNAL_API_KEY
2. 使用具有管理员权限的账号
3. 检查 API 权限配置

## 开发指南

### 添加新的检查项

1. 在 `QualityChecker` 类中添加新方法：
```python
async def check_new_item(self, client: ApiClient) -> CheckResult:
    name = "新检查项"
    details = []
    warnings = []
    # 实现检查逻辑
    return CheckResult(name, passed, total, success, details, warnings)
```

2. 在 `run_all_checks` 中注册：
```python
checks = [
    # ...
    ("新检查项", self.check_new_item),
]
```

3. 在场景 README.md 中添加预期配置

### 扩展场景支持

1. 在 `browser-scenarios/` 下创建新场景目录
2. 添加 `README.md` 和 `Quality Check Expectations` YAML 块
3. 运行质量检查工具验证

## 参考资料

- [E2E 测试文档](../README.md)
- [场景定义规范](../browser-scenarios/README.md)
- [API 客户端文档](../client/README.md)
