from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PlaybookConfig:
    playbook_id: str
    service_type_id: str
    primary_output_key: str
    alternate_output_keys: list[str] = field(default_factory=list)
    required_phases: list[str] = field(default_factory=list)
    required_trace_nodes: list[str] = field(default_factory=list)
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    docx_must_include: list[str] = field(default_factory=list)
    evidence_files: list[str] = field(default_factory=list)


PLAYBOOK_CONFIGS: dict[str, PlaybookConfig] = {
    "litigation_civil_prosecution": PlaybookConfig(
        playbook_id="litigation_civil_prosecution",
        service_type_id="civil_first_instance",
        primary_output_key="civil_complaint",
        required_phases=["kickoff", "intake", "claim_path", "execute"],
        required_trace_nodes=[
            "litigation-intake",
            "cause-recommendation",
            "document-generation",
        ],
        profile_overrides={
            "profile.facts": (
                "原告：张三E2E_GOLDEN。被告：李四E2E_GOLDEN。案由：民间借贷纠纷。"
                "事实：2023-01-01，被告向原告借款人民币100000元，约定2023-12-31前归还。"
                "到期后被告未还，原告多次催收无果。证据：借条、转账记录。"
            ),
            "profile.claims": "返还本金100000元，并按年利率6%支付逾期利息，承担诉讼费。",
            "profile.decisions.selected_documents": ["civil_complaint"],
        },
        docx_must_include=["张三E2E_GOLDEN", "李四E2E_GOLDEN", "起诉"],
        evidence_files=["iou_golden.txt"],
    ),
    "litigation_civil_defense": PlaybookConfig(
        playbook_id="litigation_civil_defense",
        service_type_id="civil_defense",
        primary_output_key="defense_statement",
        required_phases=["kickoff", "intake", "execute"],
        required_trace_nodes=[
            "complaint-analysis",
            "defense-planning",
            "document-generation",
        ],
        profile_overrides={
            "profile.facts": (
                "我方（被告）张三E2E_GOLDEN_DEF，收到原告王五E2E_GOLDEN_DEF起诉，主张民间借贷50000元及利息。"
                "我方认为双方存在其他往来款，借条真实性存疑；我方已部分还款。"
            ),
            "profile.decisions.selected_documents": ["defense_statement"],
        },
        docx_must_include=["答辩", "张三E2E_GOLDEN_DEF", "王五E2E_GOLDEN_DEF"],
        evidence_files=["opponent_complaint_golden.txt"],
    ),
    "litigation_civil_appeal_appellant": PlaybookConfig(
        playbook_id="litigation_civil_appeal_appellant",
        service_type_id="civil_appeal_appellant",
        primary_output_key="appeal_brief",
        required_phases=["kickoff", "intake", "execute"],
        required_trace_nodes=["appeal-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "上诉人：张三E2E_GOLDEN_APP。被上诉人：李四E2E_GOLDEN_APP。"
                "一审判决认定事实错误，适用法律不当，请求二审法院依法改判。"
            ),
            "profile.decisions.selected_documents": ["appeal_brief"],
        },
        docx_must_include=["上诉", "张三E2E_GOLDEN_APP"],
        evidence_files=["first_instance_judgment_golden.txt"],
    ),
    "litigation_civil_appeal_appellee": PlaybookConfig(
        playbook_id="litigation_civil_appeal_appellee",
        service_type_id="civil_appeal_appellee",
        primary_output_key="appeal_defense",
        required_phases=["kickoff", "intake", "execute"],
        required_trace_nodes=["appeal-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "被上诉人：张三E2E_GOLDEN_APPELLEE。上诉人：李四E2E_GOLDEN_APPELLEE。"
                "一审判决认定事实清楚，适用法律正确，请求二审法院驳回上诉，维持原判。"
            ),
            "profile.decisions.selected_documents": ["appeal_defense"],
        },
        docx_must_include=["答辩", "张三E2E_GOLDEN_APPELLEE"],
        evidence_files=[
            "appeal_brief_golden.txt",
            "first_instance_judgment_appellee_golden.txt",
        ],
    ),
    "litigation_criminal": PlaybookConfig(
        playbook_id="litigation_criminal",
        service_type_id="criminal_defense",
        primary_output_key="defense_opinion",
        required_phases=["kickoff", "intake", "execute"],
        required_trace_nodes=["criminal-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "当事人：张三E2E_GOLDEN_CRIM，涉嫌盗窃罪。案件处于审查起诉阶段。"
                "辩护要点：涉案金额认定存疑，当事人有自首情节，建议从轻处罚。"
            ),
            "profile.decisions.selected_documents": ["defense_opinion"],
        },
        docx_must_include=["辩护", "张三E2E_GOLDEN_CRIM"],
        evidence_files=["criminal_case_materials_golden.txt"],
    ),
    "arbitration_labor": PlaybookConfig(
        playbook_id="arbitration_labor",
        service_type_id="labor_arbitration",
        primary_output_key="labor_arbitration",
        alternate_output_keys=["labor_defense"],
        required_phases=["kickoff", "intake", "execute"],
        required_trace_nodes=["arbitration-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "申请人：张三E2E_GOLDEN_LABOR。被申请人：某公司E2E_GOLDEN_LABOR。"
                "争议类型：违法解除劳动合同。请求支付经济补偿金及赔偿金共计50000元。"
            ),
            "profile.decisions.selected_documents": ["labor_arbitration"],
        },
        docx_must_include=["仲裁", "张三E2E_GOLDEN_LABOR"],
        evidence_files=["labor_contract_golden.txt"],
    ),
    "arbitration_commercial": PlaybookConfig(
        playbook_id="arbitration_commercial",
        service_type_id="commercial_arbitration",
        primary_output_key="arbitration_application",
        alternate_output_keys=["arbitration_defense"],
        required_phases=["kickoff", "intake", "execute"],
        required_trace_nodes=["arbitration-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "申请人：甲公司E2E_GOLDEN_COMM。被申请人：乙公司E2E_GOLDEN_COMM。"
                "争议类型：买卖合同纠纷。合同约定仲裁条款，请求支付货款及违约金共计100000元。"
            ),
            "profile.decisions.selected_documents": ["arbitration_application"],
        },
        docx_must_include=["仲裁", "甲公司E2E_GOLDEN_COMM"],
        evidence_files=["commercial_contract_golden.txt"],
    ),
    "contract_review": PlaybookConfig(
        playbook_id="contract_review",
        service_type_id="contract_review",
        primary_output_key="contract_review_report",
        alternate_output_keys=["modification_suggestion"],
        required_phases=["kickoff", "qualify", "execute"],
        required_trace_nodes=[
            "contract-intake",
            "contract-review",
            "document-generation",
        ],
        profile_overrides={
            "profile.facts": (
                "请审查一份采购合同：甲方北京甲方科技E2E_GOLDEN，乙方上海乙方供应链E2E_GOLDEN。"
                "重点关注：违约金是否过高、争议解决条款、免责声明条款。"
            ),
            "profile.review_focus": "违约金、争议解决、免责声明、付款与交付风险",
        },
        docx_must_include=["合同", "甲方", "乙方"],
        evidence_files=["sample_contract_golden.txt"],
    ),
    "legal_opinion": PlaybookConfig(
        playbook_id="legal_opinion",
        service_type_id="legal_opinion",
        primary_output_key="legal_opinion",
        required_phases=["kickoff", "qualify", "execute"],
        required_trace_nodes=[
            "legal-opinion-intake",
            "legal-opinion-analysis",
            "document-generation",
        ],
        profile_overrides={
            "profile.facts": (
                "委托人：某监理公司E2E_GOLDEN_OPINION。"
                "事件：员工赵丽珍E2E_GOLDEN非因工死亡（宿舍猝死），家属主张工伤赔偿。"
                "目标：评估是否构成工伤/视同工伤，梳理公司风险与应对策略。"
            ),
            "profile.opinion_topic": "非因工死亡是否构成工伤/视同工伤及公司责任风险评估",
        },
        docx_must_include=["法律意见", "赵丽珍E2E_GOLDEN"],
        evidence_files=["background_materials_golden.txt"],
    ),
    "due_diligence": PlaybookConfig(
        playbook_id="due_diligence",
        service_type_id="due_diligence",
        primary_output_key="due_diligence_report",
        required_phases=["kickoff", "qualify", "execute"],
        required_trace_nodes=["due-diligence-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "尽调目标：某科技公司E2E_GOLDEN_DD。尽调目的：股权投资。"
                "重点关注：公司治理、知识产权、重大诉讼、财务状况。"
            ),
        },
        docx_must_include=["尽职调查", "某科技公司E2E_GOLDEN_DD"],
        evidence_files=["company_info_golden.txt"],
    ),
    "document_drafting": PlaybookConfig(
        playbook_id="document_drafting",
        service_type_id="document_drafting",
        primary_output_key="civil_complaint",
        alternate_output_keys=[
            "defense_statement",
            "appeal_brief",
            "labor_arbitration",
        ],
        required_phases=["kickoff", "qualify", "execute"],
        required_trace_nodes=["document-drafting-intake", "document-generation"],
        profile_overrides={
            "profile.facts": (
                "需要起草民事起诉状。原告：张三E2E_GOLDEN_DRAFT。被告：李四E2E_GOLDEN_DRAFT。"
                "案由：民间借贷纠纷。借款金额：50000元。"
            ),
            "profile.decisions.selected_documents": ["civil_complaint"],
        },
        docx_must_include=["起诉", "张三E2E_GOLDEN_DRAFT"],
        evidence_files=["drafting_materials_golden.txt"],
    ),
    "consultation_general": PlaybookConfig(
        playbook_id="consultation_general",
        service_type_id="legal_consultation",
        primary_output_key="",
        required_phases=["kickoff", "consulting"],
        required_trace_nodes=["consult-intake"],
        profile_overrides={
            "profile.facts": (
                "咨询问题：张三E2E_GOLDEN_CONSULT与李四E2E_GOLDEN_CONSULT存在民间借贷纠纷，"
                "借款金额30000元，已逾期半年，想了解诉讼时效和起诉流程。"
            ),
        },
        docx_must_include=[],
        evidence_files=["consult_note_golden.txt"],
    ),
}


def get_playbook_config(playbook_id: str) -> PlaybookConfig:
    if playbook_id not in PLAYBOOK_CONFIGS:
        raise ValueError(f"Unknown playbook_id: {playbook_id}")
    return PLAYBOOK_CONFIGS[playbook_id]


def all_playbook_ids() -> list[str]:
    return list(PLAYBOOK_CONFIGS.keys())
