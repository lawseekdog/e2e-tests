# LawSeekDog E2E 测试

> 端到端测试用例与测试脚本

## 目录结构

```
e2e-tests/
├── README.md
├── pytest.ini
├── requirements.txt
├── conftest.py              # 全局 fixtures
├── client/                  # API 客户端
│   └── api_client.py
├── fixtures/                # 测试数据
│   ├── sample_iou.pdf
│   ├── sample_transfer_record.txt
│   └── sample_chat_record.txt
├── tests/                   # 测试用例
│   ├── test_auth.py
│   ├── infra/               # 跨服务基础能力回归（memory/knowledge/...）
│   │   └── memory/
│   │       └── test_memory_extraction.py
│   └── lawyer_workbench/    # 律师工作台：按 service_type 分目录的端到端用例
│       ├── _support/        # 仅工具/断言（不应包含 test_*.py）
│       ├── legal_consultation/
│       ├── civil_prosecution/
│       ├── civil_defense/
│       ├── civil_appeal_appellant/
│       ├── civil_appeal_appellee/
│       ├── legal_opinion/
│       └── contract_review/
└── scripts/                 # 运维脚本
    ├── health_check.sh
    ├── smoke_test.py
    ├── run_litigation_flow_debug.py
    └── run_legal_opinion_flow_debug.py
```

## 环境准备

### 安装依赖

```bash
pip install -r requirements.txt
```

### 环境变量

```bash
cp .env.example .env
# 编辑 .env 配置测试环境
```

关键变量（本套 E2E 不 mock LLM）：

- `BASE_URL`: gateway 地址（默认 `http://localhost:18001`）
- `AI_PLATFORM_URL`: ai-engine 地址（默认 `http://localhost:18084`，用于 memory-extraction infra 测试）
- `INTERNAL_API_KEY`: 访问 `/internal/*` 路由所需（默认 `test_internal_key`，与 docker-compose 对齐）
- `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY`: 真实 LLM Key（由你的 docker-compose / .env 决定）

## 运行测试

### 运行所有测试

```bash
pytest tests/ -v
```

### 运行特定测试

```bash
# 认证测试
pytest tests/test_auth.py -v

# 律师工作台：民事起诉（原告）
pytest tests/lawyer_workbench/civil_prosecution/test_flow.py -v

# 带标记的测试
pytest tests/ -v -m e2e
pytest tests/ -v -m smoke
pytest tests/ -v -m "e2e and not slow"
pytest tests/ -v -m slow
```

### 生成报告

```bash
pytest tests/ --html=report.html --self-contained-html
```

## 测试用例

### 1. 认证测试 (`test_auth.py`)

- 登录成功
- 登录失败（错误密码）
- Token 刷新
- 获取用户信息

### 2. 律师工作台（按服务类型分目录）

- 每个 `service_type_id` 一个目录：对话/卡片推进 → 落库（matter/tasks/deliverables）→ 交付物（DOCX）→ traces/memory/knowledge 断言
- 证据材料放在各目录的 `evidence/` 下（按用例准备）

### 3. Memory Extraction 回归（`tests/infra/memory/test_memory_extraction.py`）

- 覆盖：抽取 → 写入 → recall 召回；包含 skip/PII 拦截/偏好全局化等关键回归点

## 脚本工具

### 健康检查

```bash
./scripts/health_check.sh
```

### 冒烟测试

```bash
python scripts/smoke_test.py
```

## CI/CD 集成

```yaml
# .github/workflows/e2e.yml
name: E2E Tests

on:
  schedule:
    - cron: '0 2 * * *'  # 每天凌晨2点
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -r requirements.txt
      - run: pytest tests/ -v --html=report.html
      - uses: actions/upload-artifact@v4
        with:
          name: test-report
          path: report.html
```
