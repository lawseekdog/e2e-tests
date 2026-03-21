# LawSeekDog E2E Tests

最小核心产品链路 E2E。

本仓库不再承担“大而全”的浏览器场景、基础能力回归、benchmark/golden 评分、自测 harness。这里只保留少量高价值黑盒产品流，用来验证：

- 登录与身份链路
- 民事起诉主链
- 合同审查主链
- 法律意见主链
- 模板文书起草主链

## 目录结构

```text
e2e-tests/
├── README.md
├── pytest.ini
├── requirements.txt
├── conftest.py
├── client/
│   └── api_client.py
├── fixtures/
│   ├── sample_iou.pdf
│   ├── sample_transfer_record.txt
│   └── sample_chat_record.txt
├── tests/
│   ├── test_auth.py
│   └── lawyer_workbench/
│       ├── _support/
│       ├── civil_prosecution/
│       ├── contract_review/
│       ├── document_drafting/
│       └── legal_opinion/
└── scripts/
    ├── README.md
    ├── health_check.sh
    ├── smoke_test.py
    ├── run_analysis_real_flow.py
    ├── run_contract_review_real_flow.py
    ├── run_legal_opinion_real_flow.py
    ├── run_template_draft_real_flow.py
    ├── _support/
    │   └── ... shared support modules and fixtures
    └── _debug/
        ├── assert_workbench_hardcut_results.py
        ├── run_workbench_hardcut_playwright_cli.sh
        └── ... case-specific / one-off debug runners
```

## 环境准备

```bash
pip install -r requirements.txt
cp .env.example .env
```

关键变量：

- `BASE_URL`
- `INTERNAL_API_KEY`
- `OPENROUTER_API_KEY` / `DEEPSEEK_API_KEY`

## 运行

```bash
pytest tests/ -v
```

### 关键用例

```bash
pytest tests/test_auth.py -v
pytest tests/lawyer_workbench/civil_prosecution/test_flow.py -v
pytest tests/lawyer_workbench/contract_review/test_flow.py -v
pytest tests/lawyer_workbench/legal_opinion/test_flow.py -v
pytest tests/lawyer_workbench/document_drafting/test_template_action_flow.py -v
```

### 标记

```bash
pytest tests/ -v -m e2e
pytest tests/ -v -m smoke
pytest tests/ -v -m "e2e and not slow"
pytest tests/ -v -m slow
```

## 保留范围

### 1. 认证链路

- 登录成功
- 登录失败
- 获取当前用户

### 2. 产品主链

- 民事起诉
- 合同审查
- 法律意见
- 模板文书起草

这些用例只验证产品链路：

- 对话与卡片推进
- matter 绑定与 snapshot
- deliverable 生成
- traces / timeline / workflow profile 基本可用
- 正式脚本约束：kickoff 一次，后续只答卡，不自动发送“继续”

## 不再在本仓库维护

- `tests/infra/` 基础能力回归
- benchmark / golden text 比较
- flow runner / support 自测

这些内容应迁回对应服务仓，或迁到能力评测层。

## 脚本

```bash
./scripts/health_check.sh
python scripts/smoke_test.py
python scripts/run_analysis_real_flow.py --base-url http://<host>/api/v1
python scripts/run_contract_review_real_flow.py --base-url http://<host>/api/v1
python scripts/run_legal_opinion_real_flow.py --base-url http://<host>/api/v1
python scripts/run_template_draft_real_flow.py --base-url http://<host>/api/v1 --template-id <TEMPLATE_ID>
```

说明：

- `scripts/` 顶层只保留规范主入口
- case-specific / one-off debug runner 已收口到 `scripts/_debug/`
- 排障时可以使用 `scripts/_debug/inspect_session_progress.py` 等脚本，但它们不再视为规范入口

## 维护原则

- 这里只保留少量高价值产品 E2E。
- 法律正确性、benchmark、golden cases 不再堆在本仓库。
- 基础能力回归回到 integration / unit / capability eval 层。
