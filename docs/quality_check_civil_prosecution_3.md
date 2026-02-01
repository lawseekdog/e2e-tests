## E2E 质量检查报告

### 基本信息
- **场景**: civil_prosecution
- **Session ID**: 3
- **Matter ID**: 3
- **检查时间**: 5.026285958

### 检查结果摘要

| 检查项 | 状态 | 通过/总数 |
|--------|------|-----------|
| 记忆提取 | ❌ | 0/5 |
| 记忆存储 | ❌ | 0/4 |
| 知识库命中 | ✅ | 0/2 |
| Matter 记录 | ❌ | 1/4 |
| 技能执行 | ❌ | 0/5 |
| Trace 验证 | ❌ | 0/3 |
| 阶段门控 | ❌ | 0/3 |
| 文书质量 | ❌ | 0/0 |

**总体通过率**: 3.8%

### 详细结果

#### ✅ 通过项

**知识库命中**:

#### ❌ 失败项

**记忆提取**:

**记忆存储**:

**Matter 记录**:
- ✗ matter_evidence_list_items: 期望 >= 3, 实际 0

**技能执行**:
- ✗ litigation-intake: 未执行
- ✗ evidence-analysis: 未执行
- ✗ issue-identification: 未执行
- ✗ strategy-formulation: 未执行
- ✗ document-generation: 未执行

**Trace 验证**:
- ✗ run_skill: 期望 >= 5, 实际 0
- ✗ llm_call: 期望 >= 10, 实际 0
- ✗ tool_call: 期望 >= 3, 实际 0

**阶段门控**:
- ✗ intake: 未找到
- ✗ analysis: 未找到
- ✗ document: 未找到

**文书质量**:

#### ⚠️ 警告

**记忆提取**:
- 检查失败: Client error '404 Not Found' for url 'http://localhost:18001/api/v1/memory-service/api/v1/internal/memory/users/1/facts?scope=case&case_id=3&limit=300'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404

**记忆存储**:
- 检查失败: Client error '404 Not Found' for url 'http://localhost:18001/api/v1/memory-service/api/v1/internal/memory/users/1/facts?scope=case&case_id=3&limit=300'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404

**知识库命中**:
- 知识库检查需要实际查询，暂时跳过

**Matter 记录**:
- 未知表: matter_deliverables
- 未知表: matter_parties

**文书质量**:
- 交付物无 file_id
