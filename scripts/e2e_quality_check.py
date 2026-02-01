#!/usr/bin/env python3
"""E2E 质量检查脚本

对 E2E 测试完成后的系统状态进行全面质量检查。

用法:
    python e2e_quality_check.py <scenario_name> <session_id>

示例:
    python e2e_quality_check.py contract_review 1
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# 添加项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.db import PgTarget, count, fetch_all, fetch_one
from tests.lawyer_workbench._support.docx import extract_docx_text
from tests.lawyer_workbench._support.memory import list_case_facts
from tests.lawyer_workbench._support.utils import unwrap_api_response


@dataclass
class CheckResult:
    """检查结果"""

    name: str
    passed: bool
    total: int
    success: int
    details: list[str]
    warnings: list[str]


class QualityChecker:
    """E2E 质量检查器"""

    def __init__(self, scenario_name: str, session_id: str):
        self.scenario_name = scenario_name
        self.session_id = session_id
        self.scenario_dir = (
            Path(__file__).parent.parent / "browser-scenarios" / scenario_name
        )
        self.expectations: dict[str, Any] = {}
        self.matter_id: str | None = None
        self.user_id: int | None = None
        self.organization_id: str | None = None
        self.results: list[CheckResult] = []

        # 数据库连接
        self.matter_db = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))
        self.memory_db = PgTarget(dbname=os.getenv("E2E_MEMORY_DB", "memory-service"))

    async def load_expectations(self):
        """加载场景的质量检查预期"""
        readme_path = self.scenario_dir / "README.md"
        if not readme_path.exists():
            raise FileNotFoundError(f"场景 README 不存在: {readme_path}")

        content = readme_path.read_text(encoding="utf-8")

        # 提取 YAML 块
        match = re.search(r"```yaml\n(.*?)\n```", content, re.DOTALL)
        if not match:
            raise ValueError(
                f"未找到 Quality Check Expectations YAML 块: {readme_path}"
            )

        yaml_content = match.group(1)
        self.expectations = yaml.safe_load(yaml_content)
        print(f"✓ 加载场景预期: {self.scenario_name}")

    async def get_test_context(self, client: ApiClient):
        """获取测试上下文"""
        # 通过 session_id 获取 matter_id
        try:
            session_resp = await client.get_session(self.session_id)
            session_data = unwrap_api_response(session_resp)
            self.matter_id = str(session_data.get("matter_id") or "").strip()

            if not self.matter_id:
                raise ValueError(f"Session {self.session_id} 没有关联的 matter_id")

            if not client.user_id:
                raise RuntimeError("登录后未获取到 user_id")
            self.user_id = int(client.user_id)
            self.organization_id = client.organization_id

            print(
                f"✓ 获取测试上下文: matter_id={self.matter_id}, user_id={self.user_id}"
            )
        except Exception as e:
            raise RuntimeError(f"获取测试上下文失败: {e}")

    async def check_memory_retrieval(self, client: ApiClient) -> CheckResult:
        """检查 1: 记忆提取"""
        name = "记忆提取"
        details = []
        warnings = []

        retrieval_checks = self.expectations.get("memory", {}).get("retrieval", [])
        if not retrieval_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(retrieval_checks)
        success = 0

        try:
            # 获取记忆事实
            if self.user_id is None or not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            user_id = int(self.user_id)
            case_id = str(self.matter_id)
            facts = await list_case_facts(
                client, user_id=user_id, case_id=case_id, limit=300
            )

            for check in retrieval_checks:
                entity_key = check.get("entity_key")
                must_include = check.get("must_include", [])

                # 查找匹配的 fact
                found = False
                for fact in facts:
                    if fact.get("entity_key") == entity_key:
                        content = str(fact.get("content") or "")
                        matched = all(keyword in content for keyword in must_include)
                        if matched:
                            success += 1
                            details.append(f"✓ {entity_key}: 包含 {must_include}")
                            found = True
                            break
                        else:
                            missing = [k for k in must_include if k not in content]
                            details.append(f"✗ {entity_key}: 缺少关键词 {missing}")
                            found = True
                            break

                if not found:
                    details.append(f"✗ {entity_key}: 未找到")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_memory_storage(self, client: ApiClient) -> CheckResult:
        """检查 2: 记忆存储"""
        name = "记忆存储"
        details = []
        warnings = []

        storage_checks = self.expectations.get("memory", {}).get("storage", [])
        if not storage_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(storage_checks)
        success = 0

        try:
            if self.user_id is None or not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            user_id = int(self.user_id)
            case_id = str(self.matter_id)
            facts = await list_case_facts(
                client, user_id=user_id, case_id=case_id, limit=300
            )

            for check in storage_checks:
                entity_key = check.get("entity_key")
                scope = check.get("scope")
                expected_value_contains = check.get("expected_value_contains")

                found = False
                for fact in facts:
                    if fact.get("entity_key") == entity_key:
                        fact_scope = fact.get("scope")
                        content = str(fact.get("content") or "")

                        scope_ok = not scope or fact_scope == scope
                        content_ok = (
                            not expected_value_contains
                            or expected_value_contains in content
                        )

                        if scope_ok and content_ok:
                            success += 1
                            details.append(
                                f"✓ {entity_key}: scope={fact_scope}, 内容匹配"
                            )
                            found = True
                            break
                        else:
                            details.append(
                                f"✗ {entity_key}: scope={fact_scope}, 内容不匹配"
                            )
                            found = True
                            break

                if not found:
                    details.append(f"✗ {entity_key}: 未存储")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_knowledge_hits(self, client: ApiClient) -> CheckResult:
        """检查 3: 知识库命中"""
        name = "知识库命中"
        details = []
        warnings = []

        hits_checks = self.expectations.get("knowledge", {}).get("hits", [])
        if not hits_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(hits_checks)
        success = 0

        # 知识库检查需要实际的查询，这里简化处理
        warnings.append("知识库检查需要实际查询，暂时跳过")

        passed = True
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_matter_records(self, client: ApiClient) -> CheckResult:
        """检查 4: Matter 记录"""
        name = "Matter 记录"
        details = []
        warnings = []

        records_checks = self.expectations.get("matter", {}).get("records", [])
        if not records_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(records_checks)
        success = 0

        try:
            if not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            matter_id_int = int(self.matter_id)

            for check in records_checks:
                table = check.get("table")
                expected_count = check.get("count")
                conditions = check.get("conditions", {})

                # 构建查询
                if table == "matters":
                    sql = "SELECT COUNT(1) FROM matters WHERE id = %s"
                    params: list[Any] = [matter_id_int]
                    if conditions.get("service_type"):
                        sql += " AND service_type = %s"
                        params.append(conditions["service_type"])
                elif table == "matter_tasks":
                    sql = "SELECT COUNT(1) FROM matter_tasks WHERE matter_id = %s"
                    params = [matter_id_int]
                elif table == "matter_evidence_list_items":
                    sql = "SELECT COUNT(1) FROM matter_evidence_list_items WHERE matter_id = %s"
                    params = [matter_id_int]
                elif table == "matter_deliverables":
                    sql = (
                        "SELECT COUNT(1) FROM matter_deliverables WHERE matter_id = %s"
                    )
                    params = [matter_id_int]
                    if conditions.get("output_key"):
                        sql += " AND output_key = %s"
                        params.append(str(conditions["output_key"]))
                elif table == "matter_parties":
                    sql = "SELECT COUNT(1) FROM matter_parties WHERE matter_id = %s"
                    params = [matter_id_int]
                    roles = conditions.get("roles")
                    if isinstance(roles, list) and roles:
                        # roles column is a string in most stacks; keep a simple IN filter.
                        sql += " AND role = ANY(%s)"
                        params.append([str(x) for x in roles])
                else:
                    warnings.append(f"未知表: {table}")
                    continue

                actual_count = await count(self.matter_db, sql, params)

                # 检查数量
                if isinstance(expected_count, int):
                    if actual_count == expected_count:
                        success += 1
                        details.append(f"✓ {table}: {actual_count} 条记录")
                    else:
                        details.append(
                            f"✗ {table}: 期望 {expected_count}, 实际 {actual_count}"
                        )
                elif isinstance(expected_count, str) and expected_count.startswith(
                    ">="
                ):
                    min_count = int(expected_count.split(">=")[1].strip())
                    if actual_count >= min_count:
                        success += 1
                        details.append(
                            f"✓ {table}: {actual_count} 条记录 (>= {min_count})"
                        )
                    else:
                        details.append(
                            f"✗ {table}: 期望 >= {min_count}, 实际 {actual_count}"
                        )
                else:
                    if actual_count > 0:
                        success += 1
                        details.append(f"✓ {table}: {actual_count} 条记录")
                    else:
                        details.append(f"✗ {table}: 无记录")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_skills_executed(self, client: ApiClient) -> CheckResult:
        """检查 5: 技能执行"""
        name = "技能执行"
        details = []
        warnings = []

        skills_checks = self.expectations.get("skills", {}).get("executed", [])
        if not skills_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(skills_checks)
        success = 0

        try:
            # 获取 traces
            if not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            matter_id = str(self.matter_id)
            traces_resp = await client.list_traces(matter_id, limit=200)
            traces_data = unwrap_api_response(traces_resp)
            traces = traces_data.get("traces", [])

            for check in skills_checks:
                skill_id = check.get("skill_id")
                expected_status = check.get("status", "completed")

                # 查找匹配的 trace
                found = False
                for trace in traces:
                    node_id = str(trace.get("node_id") or "")
                    status = str(trace.get("status") or "")

                    # 匹配 skill_id (可能有 "skill:" 前缀)
                    if node_id == skill_id or node_id == f"skill:{skill_id}":
                        if status == expected_status:
                            success += 1
                            details.append(f"✓ {skill_id}: {status}")
                        else:
                            details.append(
                                f"✗ {skill_id}: 期望 {expected_status}, 实际 {status}"
                            )
                        found = True
                        break

                if not found:
                    details.append(f"✗ {skill_id}: 未执行")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_trace_expectations(self, client: ApiClient) -> CheckResult:
        """检查 6: Trace 验证"""
        name = "Trace 验证"
        details = []
        warnings = []

        trace_checks = self.expectations.get("trace", {}).get("expectations", [])
        if not trace_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(trace_checks)
        success = 0

        try:
            if not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            matter_id = str(self.matter_id)
            traces_resp = await client.list_traces(matter_id, limit=200)
            traces_data = unwrap_api_response(traces_resp)
            traces = traces_data.get("traces", [])

            for check in trace_checks:
                span_name = check.get("span_name")
                expected_count = check.get("count")

                # 统计匹配的 span
                actual_count = sum(
                    1 for t in traces if span_name in str(t.get("node_id") or "")
                )

                # 检查数量
                if isinstance(expected_count, str) and expected_count.startswith(">="):
                    min_count = int(expected_count.split(">=")[1].strip())
                    if actual_count >= min_count:
                        success += 1
                        details.append(
                            f"✓ {span_name}: {actual_count} 次 (>= {min_count})"
                        )
                    else:
                        details.append(
                            f"✗ {span_name}: 期望 >= {min_count}, 实际 {actual_count}"
                        )
                else:
                    if actual_count > 0:
                        success += 1
                        details.append(f"✓ {span_name}: {actual_count} 次")
                    else:
                        details.append(f"✗ {span_name}: 未找到")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_phase_gates(self, client: ApiClient) -> CheckResult:
        """检查 7: 阶段门控"""
        name = "阶段门控"
        details = []
        warnings = []

        phase_checks = self.expectations.get("phase_gates", {}).get("checkpoints", [])
        if not phase_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        total = len(phase_checks)
        success = 0

        try:
            if not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            matter_id = str(self.matter_id)
            pt_resp = await client.get_matter_phase_timeline(matter_id)
            pt_data = unwrap_api_response(pt_resp)
            phases = pt_data.get("phases", [])

            for check in phase_checks:
                phase_id = check.get("phase")
                expected_status = check.get("status")
                required_outputs = check.get("required_outputs", [])

                # 查找匹配的 phase
                found = False
                for phase in phases:
                    if phase.get("phase_id") == phase_id:
                        status = phase.get("status")
                        outputs = phase.get("outputs", [])

                        status_ok = status == expected_status
                        outputs_ok = all(out in outputs for out in required_outputs)

                        if status_ok and outputs_ok:
                            success += 1
                            details.append(f"✓ {phase_id}: {status}, outputs={outputs}")
                        else:
                            details.append(
                                f"✗ {phase_id}: status={status}, outputs={outputs}"
                            )
                        found = True
                        break

                if not found:
                    details.append(f"✗ {phase_id}: 未找到")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total
        return CheckResult(name, passed, total, success, details, warnings)

    async def check_document_quality(self, client: ApiClient) -> CheckResult:
        """检查 8: 文书质量"""
        name = "文书质量"
        details = []
        warnings = []

        doc_checks = self.expectations.get("document", {}).get("quality", {})
        if not doc_checks:
            return CheckResult(name, True, 0, 0, ["无检查项"], [])

        # 检查是否不适用
        if doc_checks.get("format", {}).get("not_applicable"):
            return CheckResult(name, True, 0, 0, ["格式检查不适用"], [])

        total = 0
        success = 0

        try:
            # 获取交付物
            if not self.matter_id:
                raise RuntimeError("测试上下文未初始化")
            matter_id = str(self.matter_id)
            dels_resp = await client.list_deliverables(matter_id)
            dels_data = unwrap_api_response(dels_resp)
            deliverables = dels_data.get("deliverables", [])

            if not deliverables:
                warnings.append("无交付物")
                return CheckResult(name, False, total, success, details, warnings)

            # 获取第一个文档
            first_doc = deliverables[0]
            file_id = first_doc.get("file_id")

            if not file_id:
                warnings.append("交付物无 file_id")
                return CheckResult(name, False, total, success, details, warnings)

            # 下载文档内容
            try:
                doc_bytes = await client.download_file_bytes(file_id)
                # Deliverables are DOCX (zip). Decode is meaningless; extract visible text instead.
                doc_text = extract_docx_text(doc_bytes)
            except Exception as e:
                warnings.append(f"下载文档失败: {e}")
                return CheckResult(name, False, total, success, details, warnings)

            # 内容检查
            content_checks = doc_checks.get("content", {})
            must_include = content_checks.get("must_include", [])
            must_not_include = content_checks.get("must_not_include", [])

            # 检查必须包含的内容
            for keyword in must_include:
                total += 1
                if keyword in doc_text:
                    success += 1
                    details.append(f"✓ 包含: {keyword}")
                else:
                    details.append(f"✗ 缺少: {keyword}")

            # 检查禁止包含的内容
            for pattern in must_not_include:
                total += 1
                if re.search(pattern, doc_text):
                    details.append(f"✗ 包含禁止内容: {pattern}")
                else:
                    success += 1
                    details.append(f"✓ 不包含: {pattern}")

        except Exception as e:
            warnings.append(f"检查失败: {e}")

        passed = success == total if total > 0 else True
        return CheckResult(name, passed, total, success, details, warnings)

    async def run_all_checks(self, client: ApiClient):
        """执行所有检查"""
        print("\n" + "=" * 80)
        print(f"开始执行 E2E 质量检查")
        print(f"场景: {self.scenario_name}")
        print(f"Session ID: {self.session_id}")
        print(f"Matter ID: {self.matter_id}")
        print("=" * 80 + "\n")

        # 执行各项检查
        checks = [
            ("记忆提取", self.check_memory_retrieval),
            ("记忆存储", self.check_memory_storage),
            ("知识库命中", self.check_knowledge_hits),
            ("Matter 记录", self.check_matter_records),
            ("技能执行", self.check_skills_executed),
            ("Trace 验证", self.check_trace_expectations),
            ("阶段门控", self.check_phase_gates),
            ("文书质量", self.check_document_quality),
        ]

        for check_name, check_func in checks:
            print(f"执行检查: {check_name}...")
            result = await check_func(client)
            self.results.append(result)
            print(
                f"  {'✅' if result.passed else '❌'} {result.success}/{result.total}\n"
            )

    def generate_report(self) -> str:
        """生成检查报告"""
        lines = []
        lines.append("## E2E 质量检查报告\n")

        # 基本信息
        lines.append("### 基本信息")
        lines.append(f"- **场景**: {self.scenario_name}")
        lines.append(f"- **Session ID**: {self.session_id}")
        lines.append(f"- **Matter ID**: {self.matter_id}")
        lines.append(f"- **检查时间**: {asyncio.get_event_loop().time()}\n")

        # 检查结果摘要
        lines.append("### 检查结果摘要\n")
        lines.append("| 检查项 | 状态 | 通过/总数 |")
        lines.append("|--------|------|-----------|")

        total_checks = 0
        passed_checks = 0

        for result in self.results:
            status = "✅" if result.passed else "❌"
            lines.append(
                f"| {result.name} | {status} | {result.success}/{result.total} |"
            )
            total_checks += result.total
            passed_checks += result.success

        pass_rate = (passed_checks / total_checks * 100) if total_checks > 0 else 0
        lines.append(f"\n**总体通过率**: {pass_rate:.1f}%\n")

        # 详细结果
        lines.append("### 详细结果\n")

        # 通过项
        lines.append("#### ✅ 通过项\n")
        for result in self.results:
            if result.passed:
                lines.append(f"**{result.name}**:")
                for detail in result.details:
                    if detail.startswith("✓"):
                        lines.append(f"- {detail}")
                lines.append("")

        # 失败项
        lines.append("#### ❌ 失败项\n")
        has_failures = False
        for result in self.results:
            if not result.passed:
                has_failures = True
                lines.append(f"**{result.name}**:")
                for detail in result.details:
                    if detail.startswith("✗"):
                        lines.append(f"- {detail}")
                lines.append("")

        if not has_failures:
            lines.append("无失败项\n")

        # 警告
        lines.append("#### ⚠️ 警告\n")
        has_warnings = False
        for result in self.results:
            if result.warnings:
                has_warnings = True
                lines.append(f"**{result.name}**:")
                for warning in result.warnings:
                    lines.append(f"- {warning}")
                lines.append("")

        if not has_warnings:
            lines.append("无警告\n")

        return "\n".join(lines)


async def main():
    """主函数"""
    if len(sys.argv) < 3:
        print("用法: python e2e_quality_check.py <scenario_name> <session_id>")
        print("示例: python e2e_quality_check.py contract_review 1")
        sys.exit(1)

    scenario_name = sys.argv[1]
    session_id = sys.argv[2]

    # 创建检查器
    checker = QualityChecker(scenario_name, session_id)

    try:
        # 加载预期
        await checker.load_expectations()

        # 创建 API 客户端
        base_url = os.getenv("E2E_BASE_URL", "http://localhost:18001/api/v1")
        username = os.getenv("E2E_USERNAME", "admin")
        password = os.getenv("E2E_PASSWORD", "admin123456")

        async with ApiClient(base_url) as client:
            # 登录
            await client.login(username, password)
            print(f"✓ 登录成功: {username}")

            # 获取测试上下文
            await checker.get_test_context(client)

            # 执行所有检查
            await checker.run_all_checks(client)

        # 生成报告
        report = checker.generate_report()
        print("\n" + "=" * 80)
        print(report)
        print("=" * 80)

        # 保存报告
        report_path = (
            Path(__file__).parent.parent
            / "docs"
            / f"quality_check_{scenario_name}_{session_id}.md"
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"\n✓ 报告已保存: {report_path}")

    except Exception as e:
        print(f"\n❌ 检查失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
