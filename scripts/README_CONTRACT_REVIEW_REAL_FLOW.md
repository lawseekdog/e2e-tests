# 合同审查全链路（真实 LLM）运行说明

目标：不 mock，跑通 `consultations-service WS -> ai-engine -> matter/files/templates/memory`。

## 1) 后端 WS 全链路（推荐）

```bash
cd e2e-tests
python3 scripts/run_contract_review_real_flow.py \
  --base-url "http://<host>/api/v1" \
  --username "lawyer1" \
  --password "lawyer123456"
```

可选参数：

- `--contract-file /path/to/contract.docx`：指定合同文件（默认优先仓库根目录真实 docx）。
- `--no-apply-decisions`：不发 `workflow_action=contract_review_apply_decisions`。
- `--no-assert-docx`：不检查模板占位符。
- `--output-dir /path/to/output`：自定义产物目录。

产物默认输出到：`output/contract-review-chain/<timestamp>/`

- `summary.json`
- `kickoff.sse.json`
- `deliverables.json`
- `snapshot.json`
- `contract_review_report.txt`（若生成了报告 docx）
- 失败时会额外生成：`failure_diagnostics.json`、`deliverables.failure.json`、`snapshot.failure.json`

## 2) ai-engine NDJSON 直调（用于定位）

`ai-engine/scripts/debug_agent_stream.py` 已支持前缀自动探测：

```bash
python3 ai-engine/scripts/debug_agent_stream.py \
  --base-url "http://<host>/ai-platform-service" \
  --api-prefix auto \
  --message "请审查这份买卖合同并输出风险摘要" \
  --service-type-id contract_review \
  --auto-resume
```

如需强制前缀：

- `--api-prefix internal_ai` -> `/api/v1/internal/ai`
- `--api-prefix internal` -> `/api/v1/internal`
