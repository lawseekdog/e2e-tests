---
name: browser-scenarios
description: Browser Skill 验证场景集合 - 用于手动/自动化浏览器测试
---

# Browser Scenarios

本目录包含用于 `browser-automation` skill 验证的测试场景。

## 目录结构

```
browser-scenarios/
├── README.md                    # 本文件
├── civil_prosecution/           # 民事起诉场景
│   ├── README.md
│   ├── assets/
│   └── docs/
├── civil_defense/               # 民事应诉场景
│   ├── README.md
│   ├── assets/
│   └── docs/
├── civil_appeal_appellant/      # 二审上诉（上诉人）
│   ├── README.md
│   ├── assets/
│   └── docs/
├── civil_appeal_appellee/       # 二审应诉（被上诉人）
│   ├── README.md
│   ├── assets/
│   └── docs/
├── commercial_arbitration_applicant/  # 商事仲裁（申请人）
│   ├── README.md
│   ├── assets/
│   └── docs/
├── labor_arbitration_applicant/ # 劳动仲裁（申请人）
│   ├── README.md
│   ├── assets/
│   └── docs/
├── criminal_defense/            # 刑事辩护
│   ├── README.md
│   ├── assets/
│   └── docs/
├── contract_review/             # 合同审查（非诉）
│   ├── README.md
│   ├── assets/
│   └── docs/
├── legal_opinion/               # 法律意见（非诉）
│   ├── README.md
│   ├── assets/
│   └── docs/
└── document_drafting/           # 文书生成
    ├── README.md
    ├── assets/
    └── docs/
```

## 场景列表

| 场景 | service_type | 说明 |
|------|--------------|------|
| civil_prosecution | civil_prosecution | 民事起诉一审（原告） |
| civil_defense | civil_defense | 民事应诉一审（被告） |
| civil_appeal_appellant | civil_appeal_appellant | 二审上诉（上诉人） |
| civil_appeal_appellee | civil_appeal_appellee | 二审应诉（被上诉人） |
| commercial_arbitration_applicant | commercial_arbitration_applicant | 商事仲裁（申请人） |
| labor_arbitration_applicant | labor_arbitration_applicant | 劳动仲裁（申请人） |
| criminal_defense | criminal_defense | 刑事辩护 |
| contract_review | contract_review | 合同审查（非诉） |
| legal_opinion | legal_opinion | 法律意见（非诉） |
| document_drafting | document_drafting | 文书生成 |

## 使用方式

### 1. 手动验证

读取场景 README.md，按照测试步骤手动操作浏览器验证。

### 2. 使用 browser-automation skill

```bash
# 调用 skill 并传入场景路径
/browser-automation 读取 e2e-tests/browser-scenarios/civil_prosecution/README.md 并执行测试步骤
```

## README.md 格式规范

每个场景的 README.md 包含：

1. **YAML Frontmatter**: name, description, service_type, url, credentials
2. **案情描述**: 测试场景的业务背景
3. **证据文件**: assets/ 目录下的文件清单
4. **测试步骤**: 详细的操作步骤（可被 skill 解析执行）
5. **预期产物**: docs/ 目录下应生成的文件
6. **验收标准**: 测试通过的判断条件

## 产物说明

测试完成后，产物保存在各场景的 `docs/` 目录：

- `verification.png` - 验证截图
- `*.docx` - 生成的文书（如起诉状）
- `result.json` - 测试结果摘要（可选）
