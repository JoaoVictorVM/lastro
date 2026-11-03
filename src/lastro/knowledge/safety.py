"""Defesa adicional contra instruções persistidas na memória.

A inspeção textual não é um detector completo de prompt injection: a segurança depende também
dos schemas fechados, do conteúdo delimitado e das políticas determinísticas do servidor.
"""

import re

INJECTION_MESSAGE = "Knowledge proposals must be facts about the system, not instructions."
UNTRUSTED_OPEN = "<untrusted-content>"
UNTRUSTED_CLOSE = "</untrusted-content>"

_TOOL_NAMES = (
    "mission_context|submit_mission_plan|record_test_check|observe|navigate|click|fill|select|"
    "submit|screenshot|wait_for|report_finding|record_criterion_result|start_evidence_video|"
    "stop_evidence_video|ask_clarification|approval_status|end_run|generate_test_data|"
    "knowledge_query|knowledge_propose|knowledge_revalidate"
)

_DIRECTIVES = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(ignore|ignora|ignorar|disregard|forget|esque[cç]a)\b.{0,40}"
        r"\b(instructions?|instru[cç][oõ]es|rules?|regras|previous|anteriores|above|policy|"
        r"policies|pol[ií]ticas?|prompt)\b",
        r"\b(system|developer)\s+(prompt|message|instructions?)\b",
        r"\b(you|agent|assistant|ai|model|claude|llm|voc[eê]|agente|assistente|modelo)\s+"
        r"(must|should|shall|need to|have to|are required to|will now|deve|precisa|tem que)\b",
        r"\b(always|never|sempre|nunca)\s+(approve|aprove|call|chame|use|skip|pule|report|"
        r"reporte|ignore|trust|confie|click|clique|submit|envie)\b",
        r"\b(approve|aprove|aprovar|bypass|disable|desative|desabilite|skip|pule)\b.{0,40}"
        r"\b(approvals?|aprova[cç][aã]o|aprova[cç][oõ]es|polic(y|ies)|pol[ií]ticas?|guards?|"
        r"locks?|checks?|verifica[cç][aã]o)\b",
        r"\b(call|invoke|run|execute|use|chame|execute|use)\s+(the\s+|a\s+|o\s+|a\s+)?"
        r"(tool|function|ferramenta|fun[cç][aã]o|command|comando)\b",
        rf"\b({_TOOL_NAMES})\s*\(",
        r"\b(end_run|knowledge_propose|knowledge_revalidate|approval_proof|submit_mission_plan)\b",
        r"</?\s*(untrusted-content|mission-content|system|instructions?|assistant|user)\s*>",
        r"^\s*(system|assistant|user)\s*:",
    )
)


def is_directive(text: str) -> bool:
    return any(pattern.search(text) for pattern in _DIRECTIVES)


def find_directive(texts: list[str]) -> bool:
    return any(is_directive(text) for text in texts)


def escape_untrusted(text: str) -> str:
    """Impede que dado lembrado feche o delimitador de conteúdo não confiável."""
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def wrap_untrusted(text: str) -> str:
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"
