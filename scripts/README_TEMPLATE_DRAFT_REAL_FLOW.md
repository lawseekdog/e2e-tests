# 智能模板文书起草全链路（真实 LLM）运行说明

目标：在真实环境里跑通 `template_draft_start -> intake -> section_contract -> compose -> validate -> repair -> render -> sync -> finish`，并把每一步状态落盘，便于排查当前 docgen 重构后的真实链路。

## 1）推荐：线上 prod 跑法律意见书模板

```bash
cd e2e-tests

python3 scripts/run_template_draft_real_flow.py \
  --base-url "http://<prod-host>/api/v1" \
  --username "lawyer1" \
  --password "lawyer123456" \
  --template-id "<LEGAL_OPINION_TEMPLATE_ID>" \
  --debug-json
```

说明：
- 默认案情已切成“公司服务器采购合同履约争议”的法律意见书风格。
- 默认会自动上传 3 份更合理的材料：合同节选、履约时间线、往来函件摘要。
- 默认 `service_type_id=document_generation`。

若本机代理影响 WebSocket：

```bash
NO_PROXY="*" no_proxy="*" python3 scripts/run_template_draft_real_flow.py ...
```

## 2）一步一步跑指定节点

只跑到某个节点并成功退出：

```bash
python3 scripts/run_template_draft_real_flow.py \
  --base-url "http://<prod-host>/api/v1" \
  --username "lawyer1" \
  --password "lawyer123456" \
  --template-id "<LEGAL_OPINION_TEMPLATE_ID>" \
  --stop-after-node section_contract \
  --debug-json
```

可用节点：
- `intake`
- `section_contract`
- `compose`
- `hard_validate`
- `soft_validate`
- `repair`
- `render`
- `sync`
- `finish`

说明：
- 达到目标节点后，脚本会写出 `summary.json`、`node_timeline.json`、`state_snapshots/` 并返回 `0`。
- `repair` 是特殊节点；只有真正观测到 repair 才会停，不会因为“已经更靠后”而误判。

## 3）常用参数

- `--template-id`：必填，固定模板 ID。
- `--service-type-id`：默认 `document_generation`。
- `--template-name`：可选，覆盖交付标题。
- `--output-key`：默认 `template:<template_id>`。
- `--facts-file /path/to/facts.txt`：覆盖默认案情。
- `--evidence-file /path/to/file`：补充证据，可重复传参。
  - 一旦传 `--evidence-file`，就只使用你显式传入的证据，不再自动附加默认材料。
- `--poll-interval-s 2.0`：状态快照轮询间隔。
- `--max-steps 160`：主循环推进上限。
- `--max-loops 12`：单次 WS 调用 loop budget。
- `--stop-after-node <node>`：跑到指定 docgen 节点就退出。
- `--debug-json`：在 `state_snapshots` 里保留每一步的原始 API 响应。
- `--max-same-card-repeats 24`：同一卡片重复过多直接失败。
- `--max-skill-error-repeats 10`：`skill-error-analysis` 重复过多直接失败。
- `--max-stall-rounds 36`：无待办、无交付进展时直接失败。
- `--cause-anchor-file /path/to/file`：案由澄清卡死时自动补传锚点材料。
- `--no-strict-dialogue`：关闭对话严格门槛。
- `--no-strict-quality`：关闭文书质量严格门槛。
- `--min-citations 2`：法条引用最小条数。
- `--output-dir /path/to/output`：自定义输出目录。

## 4）输出目录

默认输出到：

```text
output/template-draft-chain/<timestamp>/
```

关键文件：
- `summary.json`：最终汇总，含：
  - `docgen_node_sequence`
  - `template_quality_contracts_json_exists`
  - `docgen_repair_plan_exists`
  - `docgen_repair_contracts_json_exists`
  - `quality_review_decision`
  - `soft_reason_codes`
  - `deliverable_status`
- `node_timeline.json`：节点级 timeline。
- `state_snapshots/step_001.json` ...：每一步的状态快照。
- `events.ndjson`：WS 事件流。
- `cards.json`：遇到的待办卡片。
- `deliverables.json` / `deliverables.failure.json`：交付物状态。
- `document.docx` / `document.txt`：若拿到最终文书，则落盘。
- `dialogue_quality.json`
- `document_quality.json`
- `failure_diagnostics.json`：失败时的诊断。

## 5）step 快照里有什么

每个 `state_snapshots/step_*.json` 至少包含：
- `matter_id`
- `session_id`
- `current_phase`
- `current_task_id`
- `docgen_node`
- `pending_card`
- `deliverable`
- `docgen` 标志位（`section_contract_ready/hard_validated/soft_validated/repair_required/rendered/synced`）
- `template_quality_contracts_json_exists`
- `docgen_repair_plan_exists`
- `docgen_repair_contracts_json_exists`
- `quality_review_decision`
- `soft_reason_codes`

开启 `--debug-json` 时，还会带原始：
- `workbench_snapshot`
- `workflow_snapshot`
- `phase_timeline`
- `matter_timeline`
- `pending_card`
- `deliverables`
- `traces`

## 6）节点识别来源

脚本优先使用这些真实来源做归一化判断：
- `GET /matter-service/lawyer/matters/{matterId}/workbench/snapshot`
- `GET /matter-service/matters/{matterId}/workflow`
- `GET /matter-service/lawyer/matters/{matterId}/phase-timeline`
- `GET /consultations-service/consultations/sessions/{sessionId}/pending_card`
- `GET /consultations-service/consultations/sessions/{sessionId}/traces`
- `GET /matter-service/lawyer/matters/{matterId}/deliverables`

所以它不只是“看交付有没有出来”，而是尽量判断你当前卡在：
- intake
- section_contract
- compose
- hard_validate
- soft_validate
- repair
- render
- sync
- finish

## 7）建议排查方式

- 先用 `--stop-after-node section_contract` 验证章节契约阶段能否稳定到达。
- 再用 `--stop-after-node soft_validate` 看软校验是否生成 `soft_reason_codes` / `docgen_repair_plan`。
- 最后跑到 `finish`，对照：
  - `node_timeline.json`
  - `document_quality.json`
  - `failure_diagnostics.json`
