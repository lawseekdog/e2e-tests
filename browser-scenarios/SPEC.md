# Browser Scenarios 规范文档

本文档定义了 `browser-scenarios/` 目录下测试场景的标准格式。

## 目录结构

每个场景目录应包含以下文件：

```
<scenario_name>/
├── README.md              # 场景定义（YAML frontmatter + 案情描述 + 质量检查预期）
├── config.yaml            # 公共配置（credentials/assets/timeout/output）
├── browser_steps.yaml     # 公共浏览器操作步骤
├── paths/                 # 对话路径目录（支持多种对话方案）
│   ├── progressive.yaml   # 渐进式对话路径
│   ├── one_shot.yaml      # 一次性完整输入路径
│   ├── rollback.yaml      # 回退补充证据路径
│   └── default.yaml       # 默认/单一路径（简单场景）
├── assets/                # 证据文件目录
│   └── *.txt              # 证据文件
└── docs/                  # 产物输出目录
    ├── verification.png
    └── *.docx
```

---

## 文件格式定义

### 1. config.yaml - 公共配置

```yaml
version: "1.0"
scenario: scenario_name                # 场景标识符（与目录名一致）
description: 场景描述                   # 简短描述

config:
  base_url: http://localhost:5175      # 测试目标 URL
  timeout: 120000                      # 全局超时（毫秒）
  screenshot_on_failure: true          # 失败时截图

credentials:
  username: admin                      # 登录用户名
  password: admin123456                # 登录密码

assets:                                # 证据文件映射
  evidence_file: assets/evidence.txt
  contract: assets/contract.pdf

output:                                # 预期产物
  screenshots:
    - docs/verification.png
  documents:
    - docs/civil_complaint.docx
```

### 2. browser_steps.yaml - 公共浏览器操作步骤

所有对话路径共用的浏览器操作步骤。

```yaml
version: "1.0"

steps:
  - id: browser_step_1
    name: 导航到系统首页
    tool: mcp_chrome-devtools_navigate_page
    params:
      url: "{{ config.base_url }}"
    expect:
      wait_for_text: "登录"
    on_failure: abort

  - id: browser_step_2
    name: 获取页面快照定位登录表单
    tool: mcp_chrome-devtools_take_snapshot
    expect:
      contains_elements:
        - role: textbox
        - role: button

  - id: browser_step_3
    name: 填写登录表单
    tool: mcp_chrome-devtools_fill_form
    params:
      elements:
        - uid: "{{ browser_step_2.result.username_input_uid }}"
          value: "{{ credentials.username }}"
        - uid: "{{ browser_step_2.result.password_input_uid }}"
          value: "{{ credentials.password }}"
    on_failure: retry
    retry:
      max_attempts: 2
      delay_ms: 1000

  # ... 更多浏览器步骤
```

### 3. paths/*.yaml - 对话路径定义

每个对话路径文件只包含 `interactions` 定义，不包含配置和浏览器步骤。

```yaml
id: progressive                        # 路径标识符
name: 渐进式对话                        # 路径名称
description: 一轮一轮对话，逐步补充信息   # 路径描述

interactions:
  - id: round_1
    name: 初始输入
    user_action:
      type: chat                       # chat / resume_card / upload_file
      message: "案情描述..."
      attachments: ["{{ assets.evidence_file }}"]
    
    ai_response:
      sse_events:
        - event: skill_start
          expect:
            skill_id: "litigation-intake"
        - event: delta
          expect:
            contains: ["收到", "案件"]
        - event: card
          expect:
            review_type: "clarify"
            questions_count: ">= 1"
        - event: end
          expect:
            reason: "card"
      
      final_state:
        type: card
        card:
          review_type: "clarify"
          skill_id: "litigation-intake"
          questions:
            - field_key: "profile.plaintiff.id_number"
              input_type: "text"
              required: true
      
      thinking:
        must_include: ["分析", "证据"]
        must_not_include: ["错误", "失败"]

  - id: round_2
    name: 回答卡片问题
    user_action:
      type: resume_card
      card_answers:
        - field_key: "profile.plaintiff.id_number"
          value: "110101199001011234"
    
    ai_response:
      # ...
```

---

## README.md 格式

### YAML Frontmatter

```yaml
---
name: scenario_name                    # 场景标识符（与目录名一致）
description: 场景描述                   # 简短描述
service_type: civil_first_instance     # 服务类型 ID
url: http://localhost:5175             # 测试目标 URL
credentials:
  username: admin
  password: admin123456
paths:                                 # 可用的对话路径
  - progressive
  - one_shot
  - rollback
---
```

### 必需章节

1. `# 场景标题`
2. `## 案情描述` - 业务背景
3. `## 证据文件` - assets/ 目录文件清单
4. `## 预期产物` - docs/ 目录预期输出
5. `## 验收标准` - 测试通过条件
6. `## 对话路径说明` - 各路径的用途和特点
7. `## Quality Check Expectations` - 质量检查预期数据

### Quality Check Expectations 格式

```yaml
## Quality Check Expectations

```yaml
memory:
  retrieval:                           # 记忆提取预期
    - entity_key: "party:plaintiff:primary"
      must_include: ["张三"]
  storage:                             # 记忆存储预期
    - entity_key: "party:plaintiff:primary"
      scope: case

knowledge:
  hits:                                # 知识库命中预期
    - query_type: "legal_basis"
      must_match_count: ">= 1"
      must_include_keywords: ["民间借贷", "合同法"]

matter:
  records:                             # Matter 记录预期
    - table: "matters"
      count: 1
    - table: "matter_deliverables"
      output_key: "civil_complaint"
      count: 1

skills:
  executed:                            # 技能执行预期
    - skill_id: "litigation-intake"
      status: "completed"

trace:
  expectations:                        # Trace 验证预期
    - span_name: "run_skill"
      count: ">= 3"

phase_gates:
  checkpoints:                         # 阶段门控预期
    - phase: "intake"
      status: "completed"

document:
  quality:                             # 文书质量预期
    format:
      title_centered: true
      signature_right_aligned: true
    style:
      legal_terms_check: true
    content:
      must_include: ["原告", "被告"]
      must_not_include: ["{{.*}}", "TODO"]
```
```

---

## 对话路径类型

### 1. progressive（渐进式）

一轮一轮对话，逐步补充信息。适用于信息不完整的场景。

**典型流程**：
- Round 1: 初始输入 → clarify 卡片（补充信息）
- Round 2: 回答 clarify → select 卡片（案由确认）
- Round 3: 确认案由 → select 卡片（文书选择）
- Round 4: 选择文书 → confirm 卡片（文书审核）
- Round 5: 确认文书 → 完成

### 2. one_shot（一次性完整输入）

一次性提供完整信息，跳过 clarify 卡片。适用于信息完整的场景。

**典型流程**：
- Round 1: 完整输入（含身份证号等）→ select 卡片（案由确认，必弹）
- Round 2: 确认案由 → select 卡片（文书选择，必弹）
- Round 3: 选择文书 → confirm 卡片（文书审核，必弹）
- Round 4: 确认文书 → 完成

**注意**：即使信息完整，案由确认、文书选择、文书审核卡片仍然必弹。

### 3. rollback（回退补充证据）

中途补充证据文件，触发回退重新分析。适用于测试回退逻辑的场景。

**典型流程**：
- Round 1-4: 同 progressive
- Round 5: 补充新证据文件 → 触发回退到证据分析阶段
- Round 6: 重新分析 → 可能触发案由重选
- Round 7+: 继续流程直到完成

### 4. default（默认路径）

简单场景的单一路径，适用于不需要多路径测试的场景。

---

## 必弹卡片说明

以下卡片在特定阶段必须弹出，无法跳过：

| 阶段 | Skill | 卡片类型 | 触发条件 | 是否必弹 |
|------|-------|----------|----------|----------|
| 受理 | litigation-intake | clarify | 信息不足时 | 条件弹 |
| 案由确认 | cause-recommendation | select | `profile.decisions.cause_confirmed != true` | **必弹** |
| 文书选择 | documents | select | `profile.decisions.selected_documents.length == 0` | **必弹** |
| 文书审核 | document-generation | confirm | 文书生成后 | **必弹** |

---

## AI 响应类型详解

### 1. SSE 事件类型

| 事件类型 | 说明 | 关键字段 |
|----------|------|----------|
| `skill_start` | 技能开始执行 | `skill_id` |
| `skill_end` | 技能执行完成 | `skill_id`, `success` |
| `delta` | 流式文本增量 | `content` |
| `tool_start` | 工具调用开始 | `tool_id`, `tool_name`, `arguments` |
| `tool_end` | 工具调用完成 | `tool_id`, `success`, `result` |
| `card` | 卡片中断 | `review_type`, `questions` |
| `usage` | Token 使用统计 | `prompt_tokens`, `completion_tokens` |
| `end` | 流结束 | `reason`: `stop` / `card` / `error` |
| `error` | 错误 | `code`, `message` |

### 2. 卡片类型 (review_type)

| 类型 | 说明 | 典型场景 |
|------|------|----------|
| `clarify` | 澄清/补充信息 | 缺少当事人身份证号 |
| `select` | 选择（单选/多选） | 选择案由、选择文书类型 |
| `confirm` | 确认 | 确认生成的内容 |
| `phase_done` | 阶段完成 | 受理阶段完成，进入分析阶段 |

### 3. 卡片问题输入类型 (input_type)

| 类型 | 说明 | 示例 |
|------|------|------|
| `text` | 单行文本 | 身份证号 |
| `textarea` | 多行文本 | 详细描述 |
| `number` | 数字 | 金额 |
| `date` | 日期 | 借款日期 |
| `select` | 单选 | 案由选择 |
| `multi_select` | 多选 | 文书类型选择 |
| `boolean` | 布尔 | 是否有担保 |
| `file_ids` | 文件ID列表 | 上传证据 |
| `document_review` | 文档审核 | 审核生成的文书 |

### 4. Skill 输出控制动作 (control.action)

| 动作 | 说明 | 触发条件 |
|------|------|----------|
| `continue` | 继续执行下一步 | 当前步骤成功完成 |
| `retry` | 重试当前步骤 | 输出校验失败 |
| `ask_user` | 中断等待用户输入 | 需要用户补充信息 |
| `finish` | 完成执行 | 任务完成 |

---

## 交互流程验证规则

### 1. 每轮交互必须定义

```yaml
interactions:
  - id: round_N
    name: 交互名称
    user_action:
      type: chat | resume_card | upload_file
      # ... 用户操作详情
    ai_response:
      sse_events:
        # ... SSE 事件流预期
      final_state:
        type: message | card | error
        # ... 最终状态预期
```

### 2. SSE 事件验证

```yaml
sse_events:
  - event: skill_start
    expect:
      skill_id: "litigation-intake"    # 精确匹配
  - event: delta
    expect:
      contains: ["关键词1", "关键词2"] # 包含检查
  - event: tool_end
    expect:
      success: true                    # 布尔检查
      result:
        has_file_id: true              # 结果字段检查
```

### 3. 卡片验证

```yaml
final_state:
  type: card
  card:
    review_type: "clarify"             # 卡片类型
    skill_id: "litigation-intake"      # 触发技能
    questions:
      - field_key: "profile.plaintiff.id_number"
        input_type: "text"
        required: true
      - field_key: "profile.defendant.id_number"
        input_type: "text"
        required: false
    questions_count: ">= 2"            # 问题数量检查
```

### 4. 消息验证

```yaml
final_state:
  type: message
  message:
    contains: ["关键词1", "关键词2"]
    not_contains: ["错误", "失败"]
    length: ">= 100"                   # 最小长度
```

---

## 可用的 Chrome DevTools MCP 工具

| 工具名称 | 用途 | 关键参数 |
|----------|------|----------|
| `mcp_chrome-devtools_navigate_page` | 页面导航 | `url`, `type` |
| `mcp_chrome-devtools_take_snapshot` | 获取页面快照 | - |
| `mcp_chrome-devtools_click` | 点击元素 | `uid` |
| `mcp_chrome-devtools_fill` | 填写单个输入框 | `uid`, `value` |
| `mcp_chrome-devtools_fill_form` | 批量填写表单 | `elements[]` |
| `mcp_chrome-devtools_upload_file` | 上传文件 | `uid`, `filePath` |
| `mcp_chrome-devtools_take_screenshot` | 截图 | `filePath` |
| `mcp_chrome-devtools_wait_for` | 等待文本出现 | `text`, `timeout` |
| `mcp_chrome-devtools_evaluate_script` | 执行 JS | `function` |

---

## 变量引用

支持 Jinja2 风格的变量引用，跨文件引用时使用相同的命名空间：

### 引用 config.yaml 中的配置

- `{{ config.base_url }}` - 引用 config 配置
- `{{ config.timeout }}` - 引用超时配置
- `{{ credentials.username }}` - 引用凭据
- `{{ credentials.password }}` - 引用密码
- `{{ assets.evidence_file }}` - 引用证据文件路径

### 引用 browser_steps.yaml 中的步骤结果

- `{{ browser_step_N.result.xxx }}` - 引用前序浏览器步骤结果

### 引用 paths/*.yaml 中的交互结果

- `{{ round_N.ai_response.card.id }}` - 引用前序交互的卡片 ID
- `{{ round_N.ai_response.message }}` - 引用前序交互的消息内容

---

## 完整示例

参见 `civil_prosecution/` 目录作为规范实现参考：

```
civil_prosecution/
├── README.md
├── config.yaml
├── browser_steps.yaml
├── paths/
│   ├── progressive.yaml
│   ├── one_shot.yaml
│   └── rollback.yaml
├── assets/
│   ├── iou.txt
│   ├── sample_transfer_record.txt
│   └── sample_chat_record.txt
└── docs/
    ├── verification.png
    └── civil_complaint.docx
```

---

## 质量检查 Skill 使用

测试完成后，使用以下命令进行质量检查：

```bash
/e2e-quality-check <scenario_name> <path_id> <session_id>
```

例如：
```bash
/e2e-quality-check civil_prosecution progressive ses_abc123
```

质量检查 Skill 会读取场景 README.md 中的 `Quality Check Expectations` 章节，并验证实际结果是否符合预期。
