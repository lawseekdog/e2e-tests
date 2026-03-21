"""DOCX download + content assertions."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
import zipfile
from typing import Iterable


def extract_docx_text(docx_bytes: bytes) -> str:
    """Best-effort extraction of visible text from a docx file.

    Notes:
    - Our DOCX templates use content controls (w:sdt). python-docx does not reliably
      surface w:sdtContent text, so we parse the OOXML directly.
    - For E2E assertions we only need a stable, human-visible text approximation.
    """
    if not isinstance(docx_bytes, (bytes, bytearray)) or not docx_bytes:
        return ""

    def _strip(s: str) -> str:
        return str(s or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    def _para_text(p) -> str:
        buf: list[str] = []
        for el in p.iter():
            tag = str(getattr(el, "tag", "") or "")
            if tag.endswith("}t"):
                if el.text:
                    buf.append(str(el.text))
                continue
            if tag.endswith("}tab"):
                buf.append("\t")
                continue
            if tag.endswith("}br"):
                buf.append("\n")
                continue
        return _strip("".join(buf))

    parts: list[str] = []

    try:
        with zipfile.ZipFile(BytesIO(docx_bytes)) as z:
            names = list(z.namelist())
            xml_names: list[str] = []
            for n in names:
                if n == "word/document.xml":
                    xml_names.append(n)
                elif n.startswith("word/header") and n.endswith(".xml"):
                    xml_names.append(n)
                elif n.startswith("word/footer") and n.endswith(".xml"):
                    xml_names.append(n)
                elif n in {"word/footnotes.xml", "word/endnotes.xml"}:
                    xml_names.append(n)

            import xml.etree.ElementTree as ET

            for name in xml_names:
                try:
                    root = ET.fromstring(z.read(name))
                except Exception:
                    continue
                for p in root.iter():
                    if str(getattr(p, "tag", "") or "").endswith("}p"):
                        t = _para_text(p)
                        if t:
                            parts.append(t)

            # Fallback: if there were no paragraphs, collect raw w:t (rare but harmless).
            if not parts:
                for name in xml_names:
                    try:
                        root = ET.fromstring(z.read(name))
                    except Exception:
                        continue
                    for el in root.iter():
                        if str(getattr(el, "tag", "") or "").endswith("}t") and el.text:
                            t = _strip(el.text)
                            if t:
                                parts.append(t)
    except Exception:
        return ""

    return "\n".join(parts)


def assert_docx_contains(text: str, *, must_include: Iterable[str]) -> None:
    missing: list[str] = []
    for needle in must_include:
        s = str(needle or "").strip()
        if not s:
            continue
        if s not in text:
            missing.append(s)
    if missing:
        sample = text[:2000]
        raise AssertionError(f"DOCX missing required fragments: {missing}. Extracted sample:\n{sample}")


def assert_docx_has_no_template_placeholders(text: str) -> None:
    """Catch common template placeholder leaks (jinja/docxtpl-style)."""
    t = text or ""
    bad = []
    for needle in ("{{", "}}", "{%", "%}"):
        if needle in t:
            bad.append(needle)
    if bad:
        sample = t[:2000]
        raise AssertionError(f"DOCX contains unresolved template placeholders: {bad}. Extracted sample:\n{sample}")


_LAW_CITE_RE = re.compile(r"《[^》]{2,40}》第[一二三四五六七八九十百千万0-9]{1,8}条")
_CLAUSE_REF_RE = re.compile(
    r"第\s*[一二三四五六七八九十百千万0-9]{1,6}(?:\.[0-9]{1,3})?\s*(?:条|款)|[0-9]{1,3}\.[0-9]{1,3}\s*款"
)
_NUMBERED_ITEM_RE = re.compile(
    r"(?m)^\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3}|[（(]?[一二三四五六七八九十0-9]{1,3}[)）]?)\s*[、.．]"
)

_SECTION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "title": (
        re.compile(r"合同审查意见书"),
        re.compile(r"合同审查报告"),
    ),
    "legal_basis": (
        re.compile(r"法律依据"),
        re.compile(r"主要法律依据"),
    ),
    "review_content": (
        re.compile(r"合同审查的主要内容"),
        re.compile(r"审查内容"),
        # Template variants: some use "审查范围与前提/事实基础" instead of the literal "审查内容".
        re.compile(r"审查范围"),
        re.compile(r"事实基础"),
    ),
    "issues_and_suggestions": (
        re.compile(r"主要问题及修改建议"),
        re.compile(r"问题及建议"),
        re.compile(r"修改建议"),
    ),
    "declaration": (
        re.compile(r"声明与保留"),
        re.compile(r"声明"),
    ),
    "signature": (
        re.compile(r"律师事务所"),
        re.compile(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"),
    ),
}

_PLACEHOLDER_TOKENS = ("{{", "TODO", "PLACEHOLDER")


@dataclass(frozen=True)
class ContractReviewDocxBenchmarkResult:
    score: int
    section_hits: dict[str, bool]
    legal_citation_count: int
    clause_reference_count: int
    numbered_suggestion_count: int
    has_placeholder: bool
    text_length: int
    gold_text_length: int
    length_ratio: float
    hard_gate_failures: list[str]

    @property
    def passed(self) -> bool:
        return (not self.hard_gate_failures) and self.score >= 85


def _section_hit(text: str, patterns: tuple[re.Pattern[str], ...], *, require_all: bool = False) -> bool:
    if not text:
        return False
    if require_all:
        return all(p.search(text) for p in patterns)
    return any(p.search(text) for p in patterns)


def _score_ratio(actual: int, expected: int) -> tuple[float, float]:
    if expected <= 0:
        return 1.0, 8.0
    ratio = float(actual) / float(expected)
    if 0.7 <= ratio <= 1.5:
        return ratio, 8.0
    penalty = min(8.0, abs(ratio - 1.0) * 10.0)
    return ratio, max(0.0, 8.0 - penalty)


def score_contract_review_docx_benchmark(text: str, *, gold_text: str) -> ContractReviewDocxBenchmarkResult:
    content = str(text or "")
    gold = str(gold_text or "")

    section_hits: dict[str, bool] = {}
    for name, pats in _SECTION_PATTERNS.items():
        section_hits[name] = _section_hit(content, pats, require_all=(name == "signature"))
    section_score = sum(8.0 for ok in section_hits.values() if ok)  # 6 * 8 = 48

    legal_cite_count = len(_LAW_CITE_RE.findall(content))
    legal_cite_score = min(12.0, (legal_cite_count / 3.0) * 12.0)

    clause_ref_count = len(_CLAUSE_REF_RE.findall(content))
    clause_ref_score = min(12.0, (clause_ref_count / 5.0) * 12.0)

    numbered_item_count = len(_NUMBERED_ITEM_RE.findall(content))
    numbered_item_score = min(12.0, (numbered_item_count / 8.0) * 12.0)

    has_placeholder = any(tok in content for tok in _PLACEHOLDER_TOKENS)
    placeholder_score = 0.0 if has_placeholder else 8.0

    ratio, ratio_score = _score_ratio(len(content), len(gold))

    raw_score = section_score + legal_cite_score + clause_ref_score + numbered_item_score + placeholder_score + ratio_score
    total_score = int(round(raw_score))

    hard_gate_failures: list[str] = []
    if not all(section_hits.values()):
        missing = [k for k, ok in section_hits.items() if not ok]
        hard_gate_failures.append(f"章节命中不足：缺少 {', '.join(missing)}")
    if legal_cite_count < 3:
        hard_gate_failures.append(f"法条引用不足：{legal_cite_count}（要求 >= 3）")
    if clause_ref_count < 5:
        hard_gate_failures.append(f"条款定位引用不足：{clause_ref_count}（要求 >= 5）")
    if numbered_item_count < 8:
        hard_gate_failures.append(f"编号建议条目不足：{numbered_item_count}（要求 >= 8）")
    if has_placeholder:
        hard_gate_failures.append("存在模板占位符（{{ / TODO / PLACEHOLDER）")
    if not (0.7 <= ratio <= 1.5):
        hard_gate_failures.append(f"文本长度比不达标：{ratio:.3f}（要求 0.7~1.5）")
    if total_score < 85:
        hard_gate_failures.append(f"总分不足：{total_score}（要求 >= 85）")

    return ContractReviewDocxBenchmarkResult(
        score=total_score,
        section_hits=section_hits,
        legal_citation_count=legal_cite_count,
        clause_reference_count=clause_ref_count,
        numbered_suggestion_count=numbered_item_count,
        has_placeholder=has_placeholder,
        text_length=len(content),
        gold_text_length=len(gold),
        length_ratio=ratio,
        hard_gate_failures=hard_gate_failures,
    )


def assert_contract_review_docx_benchmark(text: str, *, gold_text: str) -> ContractReviewDocxBenchmarkResult:
    result = score_contract_review_docx_benchmark(text, gold_text=gold_text)
    if result.hard_gate_failures:
        details = (
            f"score={result.score}, ratio={result.length_ratio:.3f}, "
            f"law_cites={result.legal_citation_count}, clause_refs={result.clause_reference_count}, "
            f"numbered={result.numbered_suggestion_count}"
        )
        raise AssertionError("合同审查文书质量基线未达标: " + "; ".join(result.hard_gate_failures) + f". details: {details}")
    return result


_LEGAL_OPINION_SECTION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "title": (
        re.compile(r"法律意见书|法律意见"),
        re.compile(r"赵丽珍"),
    ),
    "background": (
        re.compile(r"基本事实|事实背景|案情概述|问题概述|案情要点"),
    ),
    "non_work_injury": (
        re.compile(r"不(?:属于|构成).{0,12}工伤|不(?:属于|构成).{0,12}工亡|非因工死亡"),
        re.compile(r"视同工伤|工伤亡|三工"),
    ),
    "labor_relation": (
        re.compile(r"劳动关系|用工关系"),
        re.compile(r"社保|工伤保险|失业保险|未签.{0,8}合同"),
    ),
    "management_fault": (
        re.compile(r"管理疏漏|管理责任|监理公司"),
        re.compile(r"同吃同住|吃住在工地|管理义务"),
    ),
    "project_fault": (
        re.compile(r"安全保障义务|安全防范措施|及时救治"),
        re.compile(r"项目部|施工单位|板房|监控"),
    ),
    "drinking_fault": (
        re.compile(r"共同饮酒|陪同.{0,8}饮酒|饮酒赔偿义务"),
    ),
    "self_fault": (
        re.compile(r"自身过错|自身责任|疾病|责任比例"),
    ),
    "strategy": (
        re.compile(r"应对策略|处理建议|谈判建议|证据保全|后续建议|人道主义补偿"),
    ),
    "conclusion": (
        re.compile(r"结论|综上|意见如下"),
    ),
}

_LEGAL_OPINION_UNCERTAINTY_RE = re.compile(
    r"基于目前了解的情况|基于现有陈述|基于现有情况|需进一步核实|需结合后续证据|初步意见|供内部研判"
)


@dataclass(frozen=True)
class LegalOpinionDocxBenchmarkResult:
    score: int
    section_hits: dict[str, bool]
    legal_citation_count: int
    numbered_item_count: int
    has_uncertainty_notice: bool
    has_placeholder: bool
    text_length: int
    gold_text_length: int
    length_ratio: float
    hard_gate_failures: list[str]

    @property
    def passed(self) -> bool:
        return (not self.hard_gate_failures) and self.score >= 80


def score_legal_opinion_docx_benchmark(text: str, *, gold_text: str) -> LegalOpinionDocxBenchmarkResult:
    content = str(text or "")
    gold = str(gold_text or "")

    section_hits: dict[str, bool] = {}
    topic_hits = 0
    for name, pats in _LEGAL_OPINION_SECTION_PATTERNS.items():
        hit = _section_hit(content, pats, require_all=(name in {"title", "non_work_injury", "labor_relation", "management_fault", "project_fault"}))
        section_hits[name] = hit
        if name != "title" and hit:
            topic_hits += 1
    section_score = sum(5.0 for ok in section_hits.values() if ok)

    legal_cite_count = len(_LAW_CITE_RE.findall(content))
    legal_cite_score = min(10.0, (legal_cite_count / 2.0) * 10.0)

    numbered_item_count = len(_NUMBERED_ITEM_RE.findall(content))
    numbered_item_score = min(10.0, (numbered_item_count / 6.0) * 10.0)

    has_uncertainty_notice = bool(_LEGAL_OPINION_UNCERTAINTY_RE.search(content))
    uncertainty_score = 8.0 if has_uncertainty_notice else 0.0

    has_placeholder = any(tok in content for tok in _PLACEHOLDER_TOKENS)
    placeholder_score = 0.0 if has_placeholder else 8.0

    ratio, ratio_score = _score_ratio(len(content), len(gold))

    raw_score = section_score + legal_cite_score + numbered_item_score + uncertainty_score + placeholder_score + ratio_score
    total_score = int(round(raw_score))

    hard_gate_failures: list[str] = []
    must_hit = ["title", "non_work_injury", "labor_relation", "project_fault", "strategy", "conclusion"]
    missing = [k for k in must_hit if not section_hits.get(k)]
    if missing:
        hard_gate_failures.append(f"核心章节/主题命中不足：缺少 {', '.join(missing)}")
    if topic_hits < 7:
        hard_gate_failures.append(f"责任分析主题覆盖不足：{topic_hits}（要求 >= 7）")
    if legal_cite_count < 2:
        hard_gate_failures.append(f"法条引用不足：{legal_cite_count}（要求 >= 2）")
    if numbered_item_count < 4:
        hard_gate_failures.append(f"编号条目不足：{numbered_item_count}（要求 >= 4）")
    if not has_uncertainty_notice:
        hard_gate_failures.append("缺少无材料场景的不确定性/补证提示")
    if has_placeholder:
        hard_gate_failures.append("存在模板占位符（{{ / TODO / PLACEHOLDER）")
    if not (0.5 <= ratio <= 1.8):
        hard_gate_failures.append(f"文本长度比不达标：{ratio:.3f}（要求 0.5~1.8）")
    if total_score < 80:
        hard_gate_failures.append(f"总分不足：{total_score}（要求 >= 80）")

    return LegalOpinionDocxBenchmarkResult(
        score=total_score,
        section_hits=section_hits,
        legal_citation_count=legal_cite_count,
        numbered_item_count=numbered_item_count,
        has_uncertainty_notice=has_uncertainty_notice,
        has_placeholder=has_placeholder,
        text_length=len(content),
        gold_text_length=len(gold),
        length_ratio=ratio,
        hard_gate_failures=hard_gate_failures,
    )


def assert_legal_opinion_docx_benchmark(text: str, *, gold_text: str) -> LegalOpinionDocxBenchmarkResult:
    result = score_legal_opinion_docx_benchmark(text, gold_text=gold_text)
    if result.hard_gate_failures:
        details = (
            f"score={result.score}, ratio={result.length_ratio:.3f}, "
            f"law_cites={result.legal_citation_count}, numbered={result.numbered_item_count}, "
            f"uncertainty={result.has_uncertainty_notice}"
        )
        raise AssertionError("法律意见书质量基线未达标: " + "; ".join(result.hard_gate_failures) + f". details: {details}")
    return result
