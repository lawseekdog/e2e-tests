# AI 代理协作说明（Codex）

> 本文件用于给 Codex 类工具提供仓库上下文与约束。
>
> 若本文件与 `CLAUDE.md` 不一致，以本文件为准。

## 1. 仓库角色

- `e2e-tests` 是跨微服务的端到端回归测试仓库（pytest）。
- 目标：验证“对话 → 事项 → 产物”全链路与关键基础能力（auth/knowledge/memory/files 等）。
- 注意：本仓库通常依赖真实环境（不 mock LLM），需要正确的 `BASE_URL/INTERNAL_API_KEY/LLM_KEY`。

## 2. 修改原则（强制）

- 用例要稳定可复现：避免依赖随机性/时间敏感字段；必要时加 retry/等待策略。
- 失败要可诊断：断言信息明确；需要时保留关键响应/trace。
- 不把业务实现写进测试：测试只做黑盒验证与最小必要的内部接口准备。

## 3. 常用命令

参考 `README.md`：

- 安装：`pip install -r requirements.txt`
- 运行：`pytest tests/ -v`
