# Claude 协作上下文

> 本仓库为端到端测试（e2e-tests）。
>
> 若本文件与 `AGENTS.md` 不一致，以 `AGENTS.md` 为准。

## 约束

- 优先保证稳定性与可复现：不要把"易抖动"的断言当作硬门槛。
- 测试失败要能定位：输出应包含请求/响应的关键信息（脱敏后）。
- 只做黑盒验证；必要的 internal 准备步骤要最小化。

## 关键路径

- 测试脚本：`scripts/`
- 测试用例：`tests/`
- 测试夹具：`fixtures/`
- 客户端封装：`client/`

## 项目专属技能

参见根目录 `CLAUDE.md` 和 `.claude/commands/`
