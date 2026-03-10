from tests.lawyer_workbench._support.docx import (
    score_legal_opinion_docx_benchmark,
)


def test_legal_opinion_docx_benchmark_passes_on_rich_professional_text() -> None:
    gold_text = """
    关于赵丽珍非因工死亡事件责任分析与应对策略法律意见书
    一、基本事实与问题概述
    二、关于是否构成工伤及视同工伤的分析
    三、关于劳动关系成立的分析
    四、关于监理公司管理疏漏责任的分析
    五、关于项目部及施工单位安全保障义务的分析
    六、关于共同饮酒责任的分析
    七、关于赵丽珍自身过错及责任比例的分析
    八、应对策略与证据保全建议
    综上，本所基于目前了解的情况形成如下初步法律意见，仍需结合后续证据进一步核实。
    《工伤保险条例》第十四条。《中华人民共和国民法典》第一千一百六十五条。
    1. 不属于工作时间、工作原因，不宜认定为工伤或工亡。
    2. 已购买社保、失业保险、工伤保险，可支持劳动关系分析。
    3. 监理公司管理疏漏责任需要结合禁止同吃同住制度与实际执行情况分析。
    4. 项目部、施工单位可能存在安全保障义务问题。
    5. 陪同饮酒人员是否承担适当责任，需结合共同饮酒规则判断。
    6. 赵丽珍自身疾病与下班后饮酒行为应计入责任比例。
    """
    result = score_legal_opinion_docx_benchmark(gold_text, gold_text=gold_text)
    assert result.passed


def test_legal_opinion_docx_benchmark_rejects_short_generic_text() -> None:
    text = "赵丽珍法律意见。建议继续处理。"
    gold_text = "关于赵丽珍非因工死亡事件责任分析与应对策略法律意见书\n" * 20
    result = score_legal_opinion_docx_benchmark(text, gold_text=gold_text)
    assert not result.passed
    assert result.hard_gate_failures
