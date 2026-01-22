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
│   ├── sample_contract.pdf
│   └── sample_iou.pdf
├── tests/                   # 测试用例
│   ├── test_auth.py
│   ├── test_consultation.py
│   ├── test_matter.py
│   ├── test_litigation_flow.py
│   └── test_knowledge.py
└── scripts/                 # 运维脚本
    ├── health_check.sh
    ├── smoke_test.py
    └── load_test.py
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

## 运行测试

### 运行所有测试

```bash
pytest tests/ -v
```

### 运行特定测试

```bash
# 认证测试
pytest tests/test_auth.py -v

# 诉讼流程测试
pytest tests/test_litigation_flow.py -v

# 带标记的测试
pytest tests/ -v -m e2e
pytest tests/ -v -m smoke
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

### 2. 咨询测试 (`test_consultation.py`)

- 创建咨询会话
- 发送消息
- 升级为事项
- 卡片交互

### 3. 事项测试 (`test_matter.py`)

- 创建事项
- 更新事项
- 待办任务完成
- 阶段流转

### 4. 诉讼流程测试 (`test_litigation_flow.py`)

- 收案流程
- 案由确认
- 证据分析
- 策略规划

### 5. 知识库测试 (`test_knowledge.py`)

- 法规检索
- 案例检索
- 要素匹配

## 脚本工具

### 健康检查

```bash
./scripts/health_check.sh
```

### 冒烟测试

```bash
python scripts/smoke_test.py
```

### 负载测试

```bash
python scripts/load_test.py --users 10 --requests 100
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
