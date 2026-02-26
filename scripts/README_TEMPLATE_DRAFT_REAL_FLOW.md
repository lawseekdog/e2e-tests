# 智能模板文书起草全链路（真实 LLM）运行说明

目标：不 mock，跑通 `consultations-service WS -> ai-engine -> matter/files/templates` 的智能模板起草链路，并输出严格质检报告。

## 1) 一键运行（WS 主链路）

```bash
cd e2e-tests
python3 scripts/run_template_draft_real_flow.py \
  --base-url "http://<host>/api/v1" \
  --username "lawyer1" \
  --password "lawyer123456" \
  --template-id "<TEMPLATE_ID>"

# 若本机系统代理影响 WebSocket 握手，可追加：
NO_PROXY="*" no_proxy="*" python3 scripts/run_template_draft_real_flow.py ...
```

## 2) 常用参数

- `--template-id`：必填，固定模板 ID。
- `--service-type-id`：默认 `document_drafting`。
- `--template-name`：可选，覆盖交付标题。
- `--output-key`：默认 `template:<template_id>`。
- `--facts-file /path/to/facts.txt`：起草案情文本。
- `--evidence-file /path/to/file`：补充证据，可重复传参。
  - 传了 `--evidence-file` 时，脚本将使用你传入的证据集（不再自动附加默认证据）。
- `--max-steps 160`：流程推进轮数上限。
- `--max-loops 12`：单次 WS 调用的 loop budget。
- `--max-same-card-repeats 24`：同一待办卡片重复过多时自动失败并落盘诊断。
- `--max-skill-error-repeats 10`：`skill-error-analysis` 重复过多时自动失败。
- `--max-stall-rounds 36`：无待办且交付状态长期不变化时自动失败。
- `--cause-anchor-file /path/to/anchor.txt`：案由澄清卡死时自动补传锚点证据（达到阈值触发）。
- `--cause-anchor-repeat-threshold 3`：触发自动补传锚点证据的重复阈值。
- `--no-strict-dialogue`：关闭对话合理性严格门槛。
- `--no-strict-quality`：关闭文书质量严格门槛。
- `--min-citations 2`：法条引用最小条数。
- `--output-dir /path/to/output`：自定义产物目录。

## 3) 产物

默认输出到：`output/template-draft-chain/<timestamp>/`

- `summary.json`
- `events.ndjson`
- `cards.json`
- `deliverables.json`
- `document.docx`
- `document.txt`
- `dialogue_quality.json`
- `document_quality.json`
- 失败时额外输出：`failure_diagnostics.json`、`deliverables.failure.json`

## 4) 严格门槛（默认开启）

- 对话合理：可见响应、可交互卡片、无持续空转。
- 文书质量：无模板占位符泄漏、事实关键词覆盖、法条引用达标、交付状态为 `archived`。
