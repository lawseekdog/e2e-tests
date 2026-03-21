# e2e-tests scripts

顶层只保留规范主入口：

- `health_check.sh`
- `smoke_test.py`
- `run_analysis_real_flow.py`
- `run_contract_review_real_flow.py`
- `run_legal_opinion_real_flow.py`
- `run_template_draft_real_flow.py`

辅助目录：

- `_debug/`：一次性排障 / hardcut / case-specific runner
- `_support/`：被正式入口复用的支持模块与 fixtures

约束：

- 新增脚本时，优先补正式入口，不要把 case-specific runner 放回顶层
- 顶层不放说明型 README，不放支持模块，不放 fixture 文件
- 上述正式入口均按“kickoff 一次，后续只答卡不发继续”约束收口
