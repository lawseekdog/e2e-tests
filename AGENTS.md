# 第一优先级（强制）

- 不兜底：禁止 fallback、repair、compatibility shim、synthetic recovery、默认补全。
- 不兼容：禁止为旧协议、旧字段、旧路径保留兼容层、双写或过渡分支。
- 不止血：禁止只修表象；排查、修复、方案、重构必须优先处理根因和主控制面。
- 不保留旧路径：命中 legacy path、旧接口、旧流程、旧状态语义时，默认直接删除或硬报错，不继续维护。
- 适用范围：任何修改、排查问题、修复问题、解决方案、重构都必须遵守以上规则。

## 硬切执行细则（强制）

- 真链路脚本只能调用当前唯一入口；禁止继续保留 legacy kickoff、legacy stream、legacy resume、legacy deliverable 参数。
- 禁止双写：新旧 run status、新旧 watcher、新旧 flow runner 状态面不得并存。
- 禁止 synthetic recovery：测试脚本不得伪造 stream event、pending-card、阶段推进或补写运行状态。
- 真实排查优先读取 runtime traces、timeline、snapshot、run_status；禁止只靠 stdout 猜当前步骤。
- 旧脚本路径一旦被新统一脚本替代，必须直接删除旧参数和旧 helper。

# AI 代理协作说明（Codex）

> 本文件用于给 Codex 类工具提供仓库上下文与约束。
>
> 若本文件与 `CLAUDE.md` 不一致，以本文件为准。

## 1. 仓库角色

- `e2e-tests` 是跨微服务的端到端回归测试仓库（scripts-first，pytest 仅保留最小 support/unit）。
- 目标：验证“对话 → 事项 → 产物”全链路与关键基础能力（auth/knowledge/memory/files 等）。
- 注意：本仓库通常依赖真实环境（不 mock LLM），需要正确的 `BASE_URL/INTERNAL_API_KEY/LLM_KEY`。

## 2. 修改原则（强制）

- 用例要稳定可复现：避免依赖随机性/时间敏感字段；必要时加 retry/等待策略。
- 失败要可诊断：断言信息明确；需要时保留关键响应/trace。
- 不把业务实现写进测试：测试只做黑盒验证与最小必要的内部接口准备。
- 默认读取并使用仓库内 `./.venv` 环境，优先 `./.venv/bin/python` 和 `./.venv/bin/pytest`。
- 不要默认使用系统 `python`、系统 `pip` 或其他虚拟环境，除非任务明确要求切换环境。

## 3. 常用命令

参考 `README.md`：

- 安装：`./.venv/bin/python -m pip install -r requirements.txt`
- 主入口：
  - `./.venv/bin/python scripts/smoke_test.py`
  - `./.venv/bin/python scripts/run_analysis_real_flow.py --cards-only`
  - `./.venv/bin/python scripts/run_contract_review_real_flow.py --cards-only`
  - `./.venv/bin/python scripts/run_legal_opinion_real_flow.py --cards-only`
  - `./.venv/bin/python scripts/run_template_draft_real_flow.py --template-id <TEMPLATE_ID> --cards-only`
- pytest 仅保留最小 support/unit：`./.venv/bin/pytest tests/support/test_flow_runner_unit.py -q`

## 多智能体并行改造约定（强制）

- 默认优先并行：任务可拆分时，按脚本、fixture、support 模块、验证入口切成独立子任务并行推进，不要把可并行改造串行化。
- 默认模型：子智能体默认使用 `gpt-5.4` 且 `reasoning_effort = medium`；只有复杂链路排查时才单独提高推理强度。
- 先定边界再开工：先定义每个子智能体负责的脚本集合、环境依赖、验证口径、输出产物，禁止多个子智能体同时改同一条真链路脚本。
- 子智能体负责到底：在自己的边界内完成根因定位、脚本改造、必要验证与结果回传，不交半截子诊断。
- 主智能体只做主控面：负责统一现场、拆分任务、集成改动与最终验收，不重复做子智能体已承接的脚本改造。
- 谁改谁验证：修改哪个真链路脚本，就负责跑对应最小充分验证，不把验证全部回退给主智能体兜底。
- 命中 legacy 一次性清掉：命中旧 kickoff、旧 stream、旧 resume、旧 deliverable 参数时，默认直接删除或硬报错，不保留兼容入口。
