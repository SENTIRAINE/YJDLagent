from app.agent.llm import OpenAICompatibleChatClient
from app.agent.rag_service import RagEvidenceService
from app.agent.workflow import build_agent_graph
from app.config import Settings
from app.graph.workflow import build_rag_graph
from app.tools.spring_client import SpringToolClient


graph = build_rag_graph()

settings = Settings.from_env()
agent_graph = build_agent_graph(
    OpenAICompatibleChatClient(settings),
    RagEvidenceService(settings),
    SpringToolClient(
        settings.spring_boot_base_url,
        settings.agent_tool_service_token,
        timeout_seconds=settings.agent_tool_timeout_seconds,
    ),
)
