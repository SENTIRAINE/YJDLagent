from __future__ import annotations

from app.config import Settings
from app.graph.state import RagState
from app.rag.retriever import HybridRetriever


def build_rag_graph(settings: Settings | None = None):
    """Build the reusable retrieval subgraph used by future business agents."""
    from langgraph.graph import END, START, StateGraph

    retriever = HybridRetriever(settings or Settings.from_env())

    def retrieve(state: RagState) -> RagState:
        filters = state.get("filters", {})
        results = retriever.search(
            state["query"],
            top_k=state.get("top_k"),
            document_ids=filters.get("documentIds"),
            content_types=filters.get("contentTypes"),
        )
        return {
            "retrieval_results": [result.to_dict() for result in results],
            "context": retriever.format_context(results),
            "citations": [result.citation for result in results],
            "warnings": sorted({warning for result in results for warning in result.chunk.warnings}),
            "has_evidence": bool(results and results[0].score > 0),
        }

    builder = StateGraph(RagState)
    builder.add_node("retrieve_knowledge", retrieve)
    builder.add_edge(START, "retrieve_knowledge")
    builder.add_edge("retrieve_knowledge", END)
    return builder.compile()

