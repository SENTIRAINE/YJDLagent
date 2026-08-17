from __future__ import annotations

from typing import Any, Literal


ConversationKind = Literal["SUMMARY", "SOCIAL"]

SUMMARY_TERMS = ("总结刚才", "总结一下", "我们都说了什么", "回顾刚才", "刚才这段对话")
SOCIAL_TERMS = ("谢谢", "感谢", "干得不错", "做得不错", "可以了", "辛苦了")


def conversation_kind(query: str) -> ConversationKind | None:
    compact = "".join(query.split())
    if any(term in compact for term in SUMMARY_TERMS):
        return "SUMMARY"
    if len(compact) <= 30 and any(term in compact for term in SOCIAL_TERMS):
        return "SOCIAL"
    return None


def conversation_answer(
    kind: ConversationKind, memory: list[dict[str, Any]], state_payload: dict[str, Any] | None
) -> str:
    if kind == "SOCIAL":
        return "谢谢您的认可。您可以继续补充筛选条件，我会沿用刚才已经确认的查询范围。"

    useful = [
        item
        for item in memory
        if isinstance(item, dict) and item.get("route") in {"MAP_QUERY", "RAG_QA", "HYBRID"}
    ][-6:]
    if not useful and isinstance(state_payload, dict):
        useful = list(state_payload.get("historyDigest", []))[-6:]
    if not useful:
        return "这段对话里还没有已完成的查询或知识问答。"
    lines = []
    for item in useful:
        query = str(item.get("query", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if query:
            lines.append(f"您提出了“{query}”")
        if answer:
            lines.append(f"系统结果是：{answer}")
    return "刚才的对话可以概括为：" + "；".join(lines) + "。"

