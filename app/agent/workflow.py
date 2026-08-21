from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
import re
import time
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID, uuid4, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.channels import UntrackedValue
from langgraph.types import StreamWriter
from jsonschema import Draft202012Validator

from app.agent.errors import AgentError
from app.agent.capabilities import (
    HOUSING,
    KNOWLEDGE_REQUEST_TERMS,
    ROAD,
    ROAD_ONLY_METRIC_TERMS,
    entity_from_text,
    is_knowledge_request,
    router_known_rules,
)
from app.agent.conversation import conversation_answer, conversation_kind
from app.agent.conversation_state import contextualize_with_state, load_business_state
from app.agent.llm import OpenAICompatibleChatClient
from app.agent.map_summary import summarize_map_result
from app.agent.rag_service import RagEvidenceService
from app.tools.spring_client import SpringToolClient, ToolCallContext


logger = logging.getLogger(__name__)


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    run_id: str
    trace_id: str
    intent: Literal["MAP_QUERY", "RAG_QA", "HYBRID", "CLARIFY", "CONVERSATION"]
    route_reason: str
    clarification_reason: str
    retrieval_results: list[dict[str, Any]]
    retrieval_context: str
    citations: list[dict[str, Any]]
    catalog: dict[str, Any]
    tool_plan: list[dict[str, Any]]
    tool_outputs: Annotated[list[dict[str, Any]], UntrackedValue]
    map_result: Annotated[dict[str, Any] | None, UntrackedValue]
    map_summary: dict[str, Any] | None
    answer: str
    warnings: list[str]
    housing_search: bool
    normalized_query: str
    normalization_audit: list[dict[str, str]]
    model_operation: str
    conversation_memory: list[dict[str, Any]]
    conversation_state: dict[str, Any]
    conversation_response: str


ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["MAP_QUERY", "RAG_QA", "HYBRID", "CLARIFY", "CONVERSATION"]},
        "reason": {"type": "string"},
        "clarification": {"type": "string"},
    },
    "required": ["intent", "reason", "clarification"],
    "additionalProperties": False,
}


PLAN_ARGUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "layerId": {"type": "integer", "minimum": 0, "maximum": 5},
        "filters": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "operator": {"type": "string"},
                    "value": {"type": ["string", "number", "boolean", "null"]},
                },
                "required": ["field", "operator"],
                "additionalProperties": False,
            },
        },
        "outFields": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        "returnGeometry": {"type": "boolean"},
        "resultRecordCount": {"type": "integer", "minimum": 1, "maximum": 200},
        "resultOffset": {"type": "integer", "minimum": 0},
        "returnCount": {"type": "boolean"},
    },
    "required": ["layerId", "filters", "returnGeometry", "resultRecordCount"],
    "additionalProperties": False,
}


POINT_ENTITY_TERMS = HOUSING.aliases
ROAD_ENTITY_TERMS = ROAD.aliases
MAP_ACTION_TERMS = ("筛选", "查询", "查找", "找出", "显示", "定位", "有哪些")
POINT_FILTER_TERMS = ("房价", "覆盖度评分")
COMPARISON_TERMS = ("高于", "低于", "不高于", "不低于", "大于", "小于", "等于", "<=", ">=", "=")
EXPECTED_CATALOG_VERSION = "2026-08-21.1"
V1_TOOL_NAMES = {
    "queryMapFeatures",
    "queryMapPoints",
    "queryMapLines",
    "searchHousingCandidates",
}
DISTRICT_LAYER_LABELS = {
    "zhongshan": "中山区",
    "xigang": "西岗区",
    "shahekou": "沙河口区",
}

BUFFER_DISTANCE_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>公里|千米|km|米|m)(?![A-Za-z])",
    re.IGNORECASE,
)
PRICE_MAX_PATTERNS = (
    re.compile(
        r"(?:房价|价格|预算)\s*(?:不超过|不高于|最高(?:为)?|上限(?:为|改成|调整为)?)\s*"
        r"(?:每\s*(?:平方米|平米|㎡)\s*)?(?P<value>\d+(?:\.\d+)?)"
    ),
    re.compile(
        r"每\s*(?:平方米|平米|㎡)\s*(?:房价|价格)?\s*"
        r"(?:不超过|不高于|最高(?:为)?|上限(?:为|改成|调整为)?)\s*(?P<value>\d+(?:\.\d+)?)"
    ),
    re.compile(r"(?:房价|价格|预算)\s*(?P<value>\d+(?:\.\d+)?)\s*(?:元)?\s*(?:以内|以下|之内)"),
)
PRICE_MIN_PATTERNS = (
    re.compile(
        r"(?:房价|价格|预算)\s*(?:不低于|至少|最低(?:为)?|下限(?:为)?)\s*"
        r"(?:每\s*(?:平方米|平米|㎡)\s*)?(?P<value>\d+(?:\.\d+)?)"
    ),
    re.compile(
        r"每\s*(?:平方米|平米|㎡)\s*(?:房价|价格)?\s*"
        r"(?:不低于|至少|最低(?:为)?|下限(?:为)?)\s*(?P<value>\d+(?:\.\d+)?)"
    ),
    re.compile(r"(?:房价|价格|预算)\s*(?P<value>\d+(?:\.\d+)?)\s*(?:元)?\s*(?:以上|起)"),
)
EXPLICIT_WEIGHT_RE = re.compile(r"权重|占比|百分之|\d+(?:\.\d+)?\s*%|[一二三四五六七八九十]成")
ROAD_PERCENTILE_RE = re.compile(
    r"(?:当前(?:行政)?区域|本区|区域)?\s*(?:排名)?前\s*(?P<value>\d+(?:\.\d+)?)\s*%"
)
HOUSING_LIMIT_RE = re.compile(
    r"(?:找|挑|推荐|显示|返回|给我)\s*(?P<value>\d+)\s*(?:套|个)(?:房|住宅|小区|楼盘)?"
)
EXPLICIT_ROAD_CRITERIA_RE = re.compile(
    r"(?:GVI|NOI|WS(?:归一化)?|vegetation|noise|绿视率(?:原始值|原始分)?|"
    r"道路噪声(?:原始值|原始分)?|道路步行指数|步行指数)\s*"
    r"(?:不低于|至少|高于|大于|不高于|至多|低于|小于|>=|>|<=|<)\s*\d",
    re.IGNORECASE,
)
ROAD_CRITERIA_OPERATOR = r"(?P<operator>不低于|至少|高于|大于|不高于|至多|低于|小于|>=|>|<=|<)"
ROAD_CRITERIA_VALUE_PATTERNS = {
    "wsMin": re.compile(
        rf"(?:WS(?:归一化)?|道路步行指数|步行指数)\s*{ROAD_CRITERIA_OPERATOR}\s*"
        r"(?P<value>\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    "gviMin": re.compile(
        rf"(?:vegetation|绿视率(?:原始值|原始分)?)\s*{ROAD_CRITERIA_OPERATOR}\s*"
        r"(?P<value>\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
    "noiMax": re.compile(
        rf"(?:noise|道路噪声(?:原始值|原始分)?)\s*{ROAD_CRITERIA_OPERATOR}\s*"
        r"(?P<value>\d+(?:\.\d+)?)",
        re.IGNORECASE,
    ),
}
ROAD_CRITERIA_RANGES = {
    "wsMin": (0.0, 100.0),
    "gviMin": (0.0, 1.0),
    "noiMax": (0.0, 100.0),
}

GROUNDED_ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "answer": {"type": "string", "minLength": 1},
        "citationOrdinals": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 20},
            "uniqueItems": True,
        },
    },
    "required": ["supported", "answer", "citationOrdinals"],
    "additionalProperties": False,
}


ELDER_FRIENDLY_RESPONSE_GUIDANCE = (
    "回答对象可能是不熟悉数字产品的年长用户。请始终使用尊重、耐心、自然的中文，"
    "优先使用“您”，避免命令式、催促式或居高临下的语气。"
    "先用一两句话说清结论或当前能帮到什么，再分点解释原因；一句只表达一个重点，"
    "少用缩写和技术术语，必要时先用日常说法解释。"
    "条件不足时，要温和说明还缺什么以及为什么需要，并只给出一个容易回答的下一步；"
    "不要假设用户已经理解地图、指标或筛选规则。"
    "保持简洁，不重复用户的问题，不编造信息，也不要为了显得亲切而过度称呼。"
)


HOUSING_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["RANK", "BUFFER_FILTER"]},
        "districts": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "hardFilters": {
            "type": "object",
            "properties": {
                "priceMin": {"type": ["number", "null"], "minimum": 0},
                "priceMax": {"type": ["number", "null"], "minimum": 0},
            },
            "required": ["priceMin", "priceMax"],
            "additionalProperties": False,
        },
        "preferences": {
            "type": "object",
            "properties": {
                "price": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "level": {"const": "PREFER_LOW"},
                        "weight": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["enabled", "level", "weight"],
                    "additionalProperties": False,
                },
                "convenience": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "level": {"type": "string", "enum": ["PREFER_HIGH", "HIGH", "VERY_HIGH"]},
                        "weight": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    },
                    "required": ["enabled", "level", "weight"],
                    "additionalProperties": False,
                },
                "roadWalkability": {
                    "type": "object",
                    "properties": {
                        "enabled": {"type": "boolean"},
                        "level": {"type": "string", "enum": ["PREFER_HIGH", "HIGH", "VERY_HIGH"]},
                        "weight": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    },
                    "required": ["enabled", "level", "weight"],
                    "additionalProperties": False,
                },
            },
            "required": ["price", "convenience", "roadWalkability"],
            "additionalProperties": False,
        },
        "roadCriteria": {
            "type": "object",
            "properties": {
                "wsMin": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
                "gviMin": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                "noiMax": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
            },
            "required": ["wsMin", "gviMin", "noiMax"],
            "additionalProperties": False,
        },
        "spatial": {
            "type": "object",
            "properties": {
                "relation": {"const": "WITHIN_ROAD_BUFFER"},
                "bufferMeters": {"type": ["integer", "null"], "minimum": 20, "maximum": 2000},
            },
            "required": ["relation", "bufferMeters"],
            "additionalProperties": False,
        },
        "display": {
            "type": "object",
            "properties": {"includeRoads": {"type": "boolean"}, "includeBuffers": {"type": "boolean"}},
            "required": ["includeRoads", "includeBuffers"],
            "additionalProperties": False,
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
    },
    "required": ["mode", "districts", "hardFilters", "preferences", "roadCriteria", "spatial", "display", "limit"],
    "additionalProperties": False,
}


_QUERY_VARIANTS = {
    "中山去": "中山区",
    "西岗去": "西岗区",
    "沙河口去": "沙河口区",
    "步行指树": "步行指数",
    "步行指教": "步行指数",
}


def normalize_user_query(query: str) -> tuple[str, list[dict[str, str]]]:
    """Apply a small, auditable set of typo and colloquial normalizations."""
    normalized = query
    audit: list[dict[str, str]] = []
    for source, target in _QUERY_VARIANTS.items():
        if source in normalized:
            normalized = normalized.replace(source, target)
            audit.append({"kind": "variant", "from": source, "to": target})
    if "百来米" in normalized:
        normalized = normalized.replace("百来米", "100米")
        audit.append({"kind": "colloquial_distance", "from": "百来米", "to": "100米"})
    if "一万五" in normalized:
        normalized = normalized.replace("一万五千", "1.5万").replace("一万五", "1.5万")
        audit.append({"kind": "colloquial_price", "from": "一万五", "to": "1.5万"})
    return normalized, audit


def _explicit_map_entity(text: str) -> Literal["住宅", "道路"] | None:
    entity = entity_from_text(text)
    return "住宅" if entity == "HOUSING" else ("道路" if entity == "ROAD" else None)


def contextualize_followup_query(
    query: str,
    conversation_memory: list[dict[str, Any]],
    conversation_state: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Resolve an elliptical follow-up from committed state, then legacy memory."""
    structured = load_business_state(conversation_state or {})
    contextualized, audit = contextualize_with_state(query, structured)
    if audit:
        return contextualized, audit
    if not any(district in query for district in DISTRICT_LAYER_LABELS.values()):
        return query, []
    normalized = query.lower()
    if any(term.lower() in normalized for term in (*POINT_ENTITY_TERMS, *ROAD_ENTITY_TERMS)):
        return query, []

    for memory in reversed(conversation_memory):
        if not isinstance(memory, dict):
            continue
        entity = _explicit_map_entity(str(memory.get("query", "")))
        if entity is None:
            map_summary = memory.get("mapSummary")
            if isinstance(map_summary, dict):
                entity = _explicit_map_entity(str(map_summary.get("querySummary", "")))
        if entity is None:
            entity = _explicit_map_entity(str(memory.get("answer", "")))
        if entity is None:
            continue
        suffix = "住宅小区" if entity == "住宅" else "道路"
        return (
            f"{query.rstrip('，,。！？!? ')}，查询对象为{suffix}",
            [{"kind": "conversation_entity_inheritance", "from": entity, "to": entity}],
        )
    return query, []


def cross_layer_clarification(query: str) -> str | None:
    if is_knowledge_request(query):
        return None
    has_point_entity = any(term.lower() in query.lower() for term in POINT_ENTITY_TERMS)
    has_road_metric = any(term.lower() in query.lower() for term in ROAD_ONLY_METRIC_TERMS)
    if has_point_entity and has_road_metric:
        return (
            "您提到的 GVI、NOI、WS归一化是道路指标，不能直接用来判断住宅。"
            "请告诉我：您想查住宅，还是想查道路？"
        )
    return None


def is_housing_search_query(query: str) -> bool:
    """Recognize supported housing-road preference requests before generic routing."""
    normalized = query.lower()
    has_housing = any(term in normalized for term in POINT_ENTITY_TERMS)
    # Older users often omit a formal object noun and say "找个住得方便的地方".
    # Treat that form as housing only when it is paired with a housing preference
    # or a concrete price/district cue, so ordinary road questions stay distinct.
    has_housing = has_housing or (
        any(term in normalized for term in ("住得", "住的地方", "买得起", "合适住", "找个地方"))
        and any(term in normalized for term in ("便利", "方便", "预算", "房价", "价格", "万", "中山区", "西岗区", "沙河口区"))
    )
    # A district plus an explicit unit-price ceiling and a living preference is
    # already an unambiguous housing request, even when the user omits “房子”.
    has_housing = has_housing or (
        explicit_price_max(query) is not None
        and any(term in normalized for term in ("便利", "方便", "便宜", "步行", "路好走"))
    )
    # In the housing recommendation entry point, a paired affordability and
    # walkability preference has a single supported meaning even when an older
    # user omits an object noun (for example, "便宜点，出门路好走"). Do not
    # apply this shortcut to an explicitly road-only question.
    has_housing = has_housing or (
        not any(term in normalized for term in ROAD_ENTITY_TERMS)
        and any(term in normalized for term in ("便宜", "价格", "房价", "预算"))
        and any(term in normalized for term in ("好走", "走路", "方便", "便利", "出门", "省心"))
    )
    has_road_evidence = any(term.lower() in normalized for term in ROAD_ONLY_METRIC_TERMS) or any(
        term in normalized
        for term in (
            "高 ws",
            "很高 ws",
            "较高 ws",
            "高步行",
            "步行指数高",
            "步行条件好",
            "道路步行",
            "步行条件",
        )
    )
    has_non_walk_road_metric = any(
        term in normalized for term in ("gvi", "noi", "绿视率", "道路噪声")
    )
    if has_non_walk_road_metric and not any(character.isdigit() for character in query):
        return False
    has_spatial = any(
        term in normalized
        for term in ("附近", "周边", "道路旁", "道路边", "道路周边", "范围内", "缓冲范围", "缓冲区")
    ) or BUFFER_DISTANCE_RE.search(query) is not None
    has_housing_preference = any(
        term in normalized
        for term in ("便利", "价格尽量低", "房价尽量低", "便宜一点", "预算友好", "新步行", "好走", "出门", "省心")
    )
    has_housing_preference = has_housing_preference or any(
        term in normalized for term in ("方便", "住着方便", "生活方便", "买得起", "好走", "出门", "省心")
    )
    has_preference = any(
        term in normalized
        for term in (
            "高一点",
            "高一些",
            "尽量高",
            "高分",
            "很高",
            "较高",
            "便利",
            "步行条件",
            "步行指数高",
            "前 10%",
            "前10%",
            "推荐",
            "挑一套",
            "挑房",
            "尽量低",
            "便宜一点",
        )
    )
    has_preference = has_preference or any(
        term in normalized for term in ("方便", "住着方便", "生活方便", "买得起", "好走", "出门", "省心")
    )
    return has_housing and (
        (has_road_evidence and (has_spatial or has_preference))
        or (has_housing_preference and has_preference)
    )


def explicit_buffer_meters(query: str) -> int | None:
    """Extract a user-supplied distance without inventing the 100 m backend default."""
    if re.search(r"\d+(?:\.\d+)?\s*万\s*(?:米|m)(?![A-Za-z0-9])", query, re.IGNORECASE):
        raise AgentError(
            "INVALID_BUFFER_DISTANCE",
            "道路缓冲距离必须使用 20 至 2000 米范围内的具体数值",
            status_code=400,
        )
    values: list[int] = []
    for match in BUFFER_DISTANCE_RE.finditer(query):
        value = float(match.group("value"))
        if match.group("unit").lower() in {"公里", "千米", "km"}:
            value *= 1000
        if not value.is_integer():
            raise AgentError(
                "INVALID_BUFFER_DISTANCE",
                "道路缓冲距离必须换算为整数米",
                status_code=400,
            )
        values.append(int(value))
    if not values:
        return None
    if len(set(values)) != 1:
        raise AgentError(
            "INVALID_BUFFER_DISTANCE",
            "请求中包含冲突的道路缓冲距离",
            status_code=400,
        )
    distance = values[0]
    if not 20 <= distance <= 2000:
        raise AgentError(
            "INVALID_BUFFER_DISTANCE",
            "道路缓冲距离必须在 20 至 2000 米之间",
            status_code=400,
            details={"bufferMeters": distance},
        )
    return distance


def explicit_price_max(query: str) -> float | int | None:
    # Unit conversion belongs at the Agent boundary: Spring receives yuan per
    # square metre, while people commonly express a housing budget in 万元.
    match = re.search(
        r"(?:不超过|不高于|最高|上限|以内|以下|之内)\s*(?P<value>\d+(?:\.\d+)?)\s*万(?:元(?:/㎡|/平米|每平米)?|/㎡|/平米)?",
        query,
        re.IGNORECASE,
    )
    if match is None:
        match = re.search(
            r"(?:房价|价格|预算)\s*(?P<value>\d+(?:\.\d+)?)\s*万(?:元(?:/㎡|/平米|每平米)?|/㎡|/平米)?\s*(?:以内|以下|之内)",
            query,
            re.IGNORECASE,
        )
    if match is not None:
        value = float(match.group("value")) * 10000
        return int(value) if value.is_integer() else value
    for pattern in PRICE_MAX_PATTERNS:
        match = pattern.search(query)
        if match:
            value = float(match.group("value"))
            return int(value) if value.is_integer() else value
    return None


def explicit_price_min(query: str) -> float | int | None:
    for pattern in PRICE_MIN_PATTERNS:
        match = pattern.search(query)
        if match:
            value = float(match.group("value"))
            return int(value) if value.is_integer() else value
    return None


def has_explicit_preference_weight(query: str) -> bool:
    # A road percentile such as "当前区域前 10%" is a backend policy level,
    # not a user-supplied preference weight.
    without_road_percentile = ROAD_PERCENTILE_RE.sub("", query)
    return EXPLICIT_WEIGHT_RE.search(without_road_percentile) is not None


def explicit_road_criteria(query: str) -> dict[str, float | int]:
    criteria: dict[str, float | int] = {}
    for field, pattern in ROAD_CRITERIA_VALUE_PATTERNS.items():
        match = pattern.search(query)
        if match is None:
            continue
        operator = match.group("operator").replace(" ", "")
        unsupported_minimum = (
            "不高于" in operator
            or "至多" in operator
            or "<=" in operator
            or ("低于" in operator and "不低于" not in operator)
            or ("小于" in operator and "不小于" not in operator)
        )
        if field in {"wsMin", "gviMin"} and unsupported_minimum:
            raise AgentError(
                "INVALID_HOUSING_SEARCH_ARGUMENT",
                f"{field} 仅支持最低阈值，不能映射最高阈值条件",
                status_code=400,
            )
        unsupported_maximum = (
            "不低于" in operator
            or "至少" in operator
            or ">=" in operator
            or ("高于" in operator and "不高于" not in operator)
            or ("大于" in operator and "不大于" not in operator)
        )
        if field == "noiMax" and unsupported_maximum:
            raise AgentError(
                "INVALID_HOUSING_SEARCH_ARGUMENT",
                "noiMax 仅支持最高阈值，不能映射最低阈值条件",
                status_code=400,
            )
        value = float(match.group("value"))
        minimum, maximum = ROAD_CRITERIA_RANGES[field]
        if not minimum <= value <= maximum:
            raise AgentError(
                "INVALID_HOUSING_SEARCH_ARGUMENT",
                f"{field} 必须在 {minimum:g}-{maximum:g} 范围内",
                status_code=400,
                details={"field": field, "value": value, "minimum": minimum, "maximum": maximum},
            )
        criteria[field] = int(value) if value.is_integer() else value
    return criteria


def requested_preference_level(query: str, terms: tuple[str, ...]) -> str:
    normalized = query.lower()
    percentile = ROAD_PERCENTILE_RE.search(normalized)
    road_preference = any(
        term.lower() in {"步行指数", "道路步行", "步行条件", "ws"}
        for term in terms
    )
    if (
        road_preference
        and percentile is not None
        and float(percentile.group("value")) <= 10
    ):
        return "VERY_HIGH"
    windows = []
    for term in terms:
        start = normalized.find(term.lower())
        if start >= 0:
            windows.append(normalized[start : start + 24])
    context = " ".join(windows) if windows else normalized
    if any(phrase in context for phrase in ("非常高", "很高")):
        return "VERY_HIGH"
    if any(phrase in context for phrase in ("必须高", "务必高", "至少高")):
        return "HIGH"
    if any(phrase in context for phrase in ("高一点", "高一些", "尽量高", "较高")):
        return "PREFER_HIGH"
    if re.search(r"高(?:的)?(?:道路|路段).{0,8}(?:附近|周边)", context):
        return "HIGH"
    return "PREFER_HIGH"


def normalize_housing_search_arguments(
    planned: dict[str, Any],
    *,
    query: str,
) -> dict[str, Any]:
    """Normalize planner output while preserving backend-owned defaults and scores."""
    arguments = deepcopy(planned)
    if "新步行" in query:
        raise AgentError(
            "INVALID_HOUSING_SEARCH_ARGUMENT",
            "新步行是住宅点字段，不能替代道路 WS 参与联合搜索",
            status_code=400,
        )

    arguments["districts"] = [
        district for district in ("中山区", "西岗区", "沙河口区") if district in query
    ]

    planned_hard_filters = arguments.get("hardFilters", {})
    planned_price_min = planned_hard_filters.get("priceMin")
    planned_price_max = planned_hard_filters.get("priceMax")
    if (
        planned_price_min is not None
        and planned_price_max is not None
        and planned_price_min > planned_price_max
    ):
        raise AgentError(
            "INVALID_HOUSING_SEARCH_ARGUMENT",
            "最低房价不能高于最高房价",
            status_code=400,
        )
    # Hard filters must be grounded in the user's text; planner-supplied bounds
    # are never trusted.
    arguments["hardFilters"] = {}
    price_min = explicit_price_min(query)
    price_max = explicit_price_max(query)
    if price_min is not None:
        arguments["hardFilters"]["priceMin"] = price_min
    if price_max is not None:
        arguments["hardFilters"]["priceMax"] = price_max
    if price_min is not None and price_max is not None and price_min > price_max:
        raise AgentError(
            "INVALID_HOUSING_SEARCH_ARGUMENT",
            "最低房价不能高于最高房价",
            status_code=400,
        )

    preferences = arguments["preferences"]
    requested_preferences = {
        "price": any(
            term in query
            for term in ("价格尽量低", "房价尽量低", "便宜一点", "便宜点", "便宜些", "预算友好")
        ),
        "convenience": any(term in query for term in ("便利", "方便", "省心")),
        "roadWalkability": any(
            term.lower() in query.lower()
            for term in ROAD_ONLY_METRIC_TERMS
            + ("高步行", "步行条件", "道路步行", "周边道路", "附近道路", "好走", "走路")
        ),
    }
    for name, requested in requested_preferences.items():
        preferences[name]["enabled"] = requested
    preferences["price"]["level"] = "PREFER_LOW"
    if requested_preferences["convenience"]:
        preferences["convenience"]["level"] = requested_preference_level(
            query, ("便利度", "便利")
        )
    if requested_preferences["roadWalkability"]:
        preferences["roadWalkability"]["level"] = requested_preference_level(
            query, ("步行指数", "道路步行", "步行条件", "ws")
        )
        hard_road_level = preferences["roadWalkability"]["level"] in {"HIGH", "VERY_HIGH"}
        spatial_request = any(
            term in query
            for term in ("附近", "周边", "道路旁", "道路边", "范围内", "缓冲范围", "缓冲区")
        ) or BUFFER_DISTANCE_RE.search(query) is not None
        arguments["mode"] = "BUFFER_FILTER" if hard_road_level and spatial_request else "RANK"
    if not has_explicit_preference_weight(query):
        for name, requested in requested_preferences.items():
            if requested:
                preferences[name]["weight"] = None

    arguments["display"]["includeRoads"] = requested_preferences["roadWalkability"]
    arguments["display"]["includeBuffers"] = requested_preferences["roadWalkability"]
    limit_match = HOUSING_LIMIT_RE.search(query)
    arguments["limit"] = int(limit_match.group("value")) if limit_match else 20
    if not 1 <= arguments["limit"] <= 50:
        raise AgentError(
            "INVALID_HOUSING_SEARCH_ARGUMENT",
            "候选住宅数量必须在 1 至 50 之间",
            status_code=400,
            details={"limit": arguments["limit"]},
        )

    distance = explicit_buffer_meters(query)
    planned_distance = arguments["spatial"].get("bufferMeters")
    if distance is None:
        if planned_distance is not None:
            raise AgentError(
                "INVALID_HOUSING_SEARCH_ARGUMENT",
                "用户未指定道路缓冲距离，Planner 不得自行生成距离",
                status_code=400,
            )
        arguments["spatial"].pop("bufferMeters", None)
    else:
        arguments["spatial"]["bufferMeters"] = distance

    # Numeric road criteria are allowed only when that exact road metric and a
    # number appear in the request. This prevents a structured-output planner
    # from silently inventing absolute thresholds for fuzzy preferences.
    arguments["roadCriteria"] = explicit_road_criteria(query)

    names = ("price", "convenience", "roadWalkability")
    enabled = [name for name in names if preferences[name]["enabled"]]
    if not enabled:
        raise AgentError(
            "INVALID_HOUSING_SEARCH_ARGUMENT",
            "至少需要启用一项购房偏好",
            status_code=400,
        )
    for name in names:
        if not preferences[name]["enabled"]:
            preferences[name]["weight"] = 0

    if len(enabled) == 1:
        preferences[enabled[0]]["weight"] = 1
    else:
        active_weights = [preferences[name].get("weight") for name in enabled]
        if all(weight is None for weight in active_weights):
            # The live Catalog requires price.weight but leaves the two
            # non-price preferences optional. Keep that backend default for
            # convenience plus walkability; otherwise use a neutral split for
            # multiple implicit preferences rather than asking a second time.
            if set(enabled) == {"convenience", "roadWalkability"}:
                for name in enabled:
                    preferences[name].pop("weight", None)
            else:
                default_weight = 1 / len(enabled)
                for name in enabled:
                    preferences[name]["weight"] = default_weight
        elif any(weight is None for weight in active_weights):
            raise AgentError(
                "INVALID_HOUSING_SEARCH_ARGUMENT",
                "偏好权重必须同时省略或全部提供",
                status_code=400,
            )
        else:
            total_weight = sum(float(weight) for weight in active_weights)
            if total_weight <= 0:
                raise AgentError(
                    "INVALID_HOUSING_SEARCH_ARGUMENT",
                    "启用偏好的权重之和必须大于 0",
                    status_code=400,
                )
            for name in enabled:
                preferences[name]["weight"] = float(preferences[name]["weight"]) / total_weight

    if arguments["mode"] == "BUFFER_FILTER":
        road = preferences["roadWalkability"]
        if not road["enabled"] or road["level"] not in {"HIGH", "VERY_HIGH"}:
            raise AgentError(
                "INVALID_HOUSING_SEARCH_ARGUMENT",
                "BUFFER_FILTER 必须启用 HIGH 或 VERY_HIGH 道路偏好",
                status_code=400,
            )
    return arguments


def deterministic_housing_search_arguments(query: str) -> dict[str, Any] | None:
    if has_explicit_preference_weight(query) or EXPLICIT_ROAD_CRITERIA_RE.search(query):
        return None
    return normalize_housing_search_arguments(
        {
            "mode": "RANK",
            "districts": [],
            "hardFilters": {"priceMin": None, "priceMax": None},
            "preferences": {
                "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
                "convenience": {
                    "enabled": False,
                    "level": "PREFER_HIGH",
                    "weight": None,
                },
                "roadWalkability": {
                    "enabled": False,
                    "level": "PREFER_HIGH",
                    "weight": None,
                },
            },
            "roadCriteria": {"wsMin": None, "gviMin": None, "noiMax": None},
            "spatial": {"relation": "WITHIN_ROAD_BUFFER", "bufferMeters": None},
            "display": {"includeRoads": False, "includeBuffers": False},
            "limit": 20,
        },
        query=query,
    )


def stable_tool_call_id(run_id: str, tool_name: str, ordinal: int) -> str:
    """Keep one idempotency key for one logical Tool call within a Run."""
    return str(uuid5(UUID(run_id), f"{ordinal}:{tool_name}"))


def is_frozen_point_map_query(query: str) -> bool:
    """Recognize unambiguous point queries covered by the frozen district mapping."""
    normalized = query.lower()
    return (
        any(label in query for label in DISTRICT_LAYER_LABELS.values())
        and any(term.lower() in normalized for term in POINT_ENTITY_TERMS)
        and any(term.lower() in normalized for term in MAP_ACTION_TERMS)
        and any(term.lower() in normalized for term in POINT_FILTER_TERMS)
        and (
            any(term.lower() in normalized for term in COMPARISON_TERMS)
            or any(character.isdigit() for character in query)
        )
        and not any(term.lower() in normalized for term in KNOWLEDGE_REQUEST_TERMS)
    )


def is_explicit_road_map_query(query: str) -> bool:
    """Route user-supplied road thresholds without inventing units or defaults."""
    normalized = query.lower()
    explicit_metrics = ("gvi", "noi", "ws", "shape_length")
    return (
        any(label in query for label in DISTRICT_LAYER_LABELS.values())
        and any(term.lower() in normalized for term in ROAD_ENTITY_TERMS)
        and any(term.lower() in normalized for term in MAP_ACTION_TERMS)
        and any(term in normalized for term in explicit_metrics)
        and (
            any(term.lower() in normalized for term in COMPARISON_TERMS)
            or any(character.isdigit() for character in query)
        )
        and not any(term.lower() in normalized for term in KNOWLEDGE_REQUEST_TERMS)
    )


def normalize_catalog(response: dict[str, Any]) -> dict[str, Any]:
    catalog = response.get("data", response)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise AgentError("TOOL_EXECUTION_FAILED", "Tool Catalog 返回格式非法", status_code=500)
    if not catalog.get("version"):
        raise AgentError("TOOL_EXECUTION_FAILED", "Tool Catalog 缺少版本", status_code=500)
    if catalog["version"] != EXPECTED_CATALOG_VERSION:
        raise AgentError(
            "TOOL_CATALOG_VERSION_MISMATCH",
            "Tool Catalog 版本不匹配，已阻止使用旧版工具契约",
            status_code=503,
            retryable=True,
            details={"expectedVersion": EXPECTED_CATALOG_VERSION, "actualVersion": catalog["version"]},
        )
    names: set[str] = set()
    for tool in catalog["tools"]:
        name = tool.get("name")
        if not isinstance(name, str) or name in names:
            raise AgentError("TOOL_EXECUTION_FAILED", "Tool Catalog 工具名缺失或重复", status_code=500)
        if name not in V1_TOOL_NAMES:
            raise AgentError("TOOL_EXECUTION_FAILED", "Tool Catalog 包含 v1 契约外工具", status_code=500)
        if tool.get("sideEffect") is not False:
            raise AgentError("TOOL_EXECUTION_FAILED", "v1 禁止加载有副作用的 Tool", status_code=500)
        timeout_ms = tool.get("timeoutMs")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise AgentError("TOOL_EXECUTION_FAILED", "Tool Catalog timeoutMs 非法", status_code=500)
        try:
            Draft202012Validator.check_schema(tool["inputSchema"])
            Draft202012Validator.check_schema(tool["outputSchema"])
        except Exception as exc:
            raise AgentError("TOOL_EXECUTION_FAILED", "Tool Catalog Schema 非法", status_code=500) from exc
        names.add(name)
    return catalog


def compact_catalog(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for tool in catalog["tools"]:
        layers = []
        for branch in tool.get("inputSchema", {}).get("oneOf", []):
            properties = branch.get("properties", {})
            filters = []
            for filter_branch in properties.get("filters", {}).get("items", {}).get("oneOf", []):
                filter_properties = filter_branch.get("properties", {})
                filters.append(
                    {
                        "field": filter_properties.get("field", {}).get("const"),
                        "operators": filter_properties.get("operator", {}).get("enum", []),
                    }
                )
            layers.append(
                {
                    "layerId": properties.get("layerId", {}).get("const"),
                    "title": branch.get("title"),
                    "district": DISTRICT_LAYER_LABELS.get(
                        str(branch.get("title", "")).lower().split(" ", 1)[0].split("_", 1)[0]
                    ),
                    "filters": filters,
                }
            )
        compact.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description"),
                "layers": layers,
                "inputSchema": tool.get("inputSchema")
                if tool.get("name") == "searchHousingCandidates"
                else None,
            }
        )
    return compact


def plan_schema(tool_names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "toolCalls": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "toolName": {"type": "string", "enum": tool_names},
                        "arguments": PLAN_ARGUMENT_SCHEMA,
                    },
                    "required": ["toolName", "arguments"],
                    "additionalProperties": False,
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["toolCalls", "summary"],
        "additionalProperties": False,
    }


def _object_id_field(attributes: dict[str, Any]) -> str | None:
    for field in ("OBJECTID_12", "OBJECTID", "id"):
        if field in attributes and attributes[field] is not None:
            return field
    return None


def _valid_geometry(geometry: Any, geometry_type: str) -> bool:
    if not isinstance(geometry, dict):
        return False
    spatial_reference = geometry.get("spatialReference")
    if not isinstance(spatial_reference, dict) or spatial_reference.get("wkid") != 4326:
        return False
    if geometry_type == "point":
        return _valid_coordinate((geometry.get("x"), geometry.get("y")))
    if geometry_type == "polyline":
        return _valid_paths(geometry.get("paths"))
    return False


def _valid_coordinate(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and all(
            isinstance(coordinate, (int, float))
            and not isinstance(coordinate, bool)
            and math.isfinite(coordinate)
            for coordinate in value[:2]
        )
    )


def _valid_paths(paths: Any) -> bool:
    return (
        isinstance(paths, list)
        and bool(paths)
        and all(
            isinstance(path, list)
            and len(path) >= 2
            and all(_valid_coordinate(coordinate) for coordinate in path)
            for path in paths
        )
    )


def _valid_rings(rings: Any) -> bool:
    return (
        isinstance(rings, list)
        and bool(rings)
        and all(
            isinstance(ring, list)
            and len(ring) >= 4
            and all(_valid_coordinate(coordinate) for coordinate in ring)
            and ring[0][:2] == ring[-1][:2]
            for ring in rings
        )
    )


def validate_tool_result_consistency(
    tool_name: str, arguments: dict[str, Any], result: dict[str, Any]
) -> None:
    if tool_name == "searchHousingCandidates":
        # The joint-search contract returns business-level collections rather
        # than the single-layer shape used by queryMapPoints/queryMapLines.
        return
    if result.get("layerId") != arguments.get("layerId"):
        raise AgentError(
            "TOOL_EXECUTION_FAILED", "Tool 返回图层与调用参数不一致", status_code=500
        )
    geometry_type = result.get("geometryType")
    if tool_name == "queryMapPoints" and geometry_type != "point":
        raise AgentError("TOOL_EXECUTION_FAILED", "点 Tool 返回了非点几何", status_code=500)
    if tool_name == "queryMapLines" and geometry_type != "polyline":
        raise AgentError("TOOL_EXECUTION_FAILED", "线 Tool 返回了非线几何", status_code=500)
    features = result.get("features", [])
    if not isinstance(features, list) or int(result.get("total", -1)) < len(features):
        raise AgentError(
            "TOOL_EXECUTION_FAILED", "Tool 返回的 total 与 features 不一致", status_code=500
        )


def build_map_result(
    tool_plan: list[dict[str, Any]], tool_outputs: list[dict[str, Any]]
) -> dict[str, Any]:
    result_sets: list[dict[str, Any]] = []
    warnings: list[str] = []
    applied_filters: list[dict[str, Any]] = []
    total_returned = 0

    for call, output in zip(tool_plan, tool_outputs, strict=True):
        result = output["result"]
        for item in call["arguments"].get("filters", []):
            applied_filters.append(dict(item))
        if not result.get("features"):
            continue

        layer_id = int(result["layerId"])
        geometry_type = str(result["geometryType"])
        mapped_features = []
        object_id_field: str | None = None
        for feature in result["features"]:
            if total_returned >= 200:
                warnings.append("地图结果合计超过 200 条，已按契约截断")
                break
            attributes = feature.get("attributes")
            geometry = feature.get("geometry")
            if not _valid_geometry(geometry, geometry_type):
                raise AgentError(
                    "TOOL_EXECUTION_FAILED",
                    f"图层 {layer_id} 返回了缺失或非法的 WGS84 几何",
                    status_code=500,
                )
            if not isinstance(attributes, dict):
                warnings.append(f"图层 {layer_id} 中存在缺少属性的记录，已忽略")
                continue
            current_id_field = _object_id_field(attributes)
            if current_id_field is None:
                warnings.append(f"图层 {layer_id} 中存在缺少对象 ID 的记录，已忽略")
                continue
            object_id_field = object_id_field or current_id_field
            if current_id_field != object_id_field:
                warnings.append(f"图层 {layer_id} 的对象 ID 字段不一致，已忽略异常记录")
                continue
            mapped_features.append(
                {
                    "id": f"{layer_id}:{attributes[object_id_field]}",
                    "attributes": attributes,
                    "geometry": geometry,
                }
            )
            total_returned += 1

        if mapped_features and object_id_field:
            exceeded = bool(result.get("exceededTransferLimit")) or len(mapped_features) < int(result["total"])
            if exceeded:
                warnings.append(f"图层 {layer_id} 仅返回部分结果")
            result_sets.append(
                {
                    "toolCallId": call["toolCallId"],
                    "role": "PRIMARY_RESULTS",
                    "layerId": layer_id,
                    "layerName": result["layerName"],
                    "geometryType": geometry_type,
                    "spatialReference": {"wkid": 4326},
                    "total": int(result["total"]),
                    "returned": len(mapped_features),
                    "exceededTransferLimit": exceeded,
                    "objectIdField": object_id_field,
                    "features": mapped_features,
                }
            )

    return {
        "queryId": str(uuid4()),
        "toolCallIds": [call["toolCallId"] for call in tool_plan],
        "mode": "replace",
        "querySummary": f"地图查询返回 {total_returned} 个可展示要素",
        "appliedFilters": applied_filters,
        "resultSets": result_sets,
        "overlays": [],
        "display": {
            "fitBounds": bool(result_sets),
            "paddingPx": 48,
            "maxZoom": 17,
            "layerOrder": ["PRIMARY_RESULTS"],
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def concise_map_result_answer(map_result: dict[str, Any]) -> str:
    """Describe a successful map query without re-interpreting Tool output."""
    result_sets = map_result.get("resultSets") or map_result.get("resultCounts", [])
    housing_count = sum(
        int(item.get("returned", 0))
        for item in result_sets
        if item.get("role") == "HOUSING_CANDIDATES"
    )
    primary_point_count = sum(
        int(item.get("returned", 0))
        for item in result_sets
        if item.get("role") == "PRIMARY_RESULTS" and item.get("geometryType") == "point"
    )
    primary_line_count = sum(
        int(item.get("returned", 0))
        for item in result_sets
        if item.get("role") == "PRIMARY_RESULTS" and item.get("geometryType") == "polyline"
    )
    related_road_count = sum(
        int(item.get("returned", 0))
        for item in result_sets
        if item.get("role") == "CONTRIBUTING_ROADS"
    )
    is_housing_search = str(map_result.get("queryId", "")).startswith("housing-")

    truncated = any(bool(item.get("exceededTransferLimit")) for item in result_sets)
    qualifier = "当前显示前" if truncated else "已找到"
    if housing_count:
        answer = f"查询完成：{qualifier} {housing_count} 个符合条件的候选小区，已显示在地图和左侧结果中。"
        if related_road_count:
            answer += f"同时显示 {related_road_count} 条相关道路。"
        return answer
    if primary_point_count and not primary_line_count:
        return f"查询完成：{qualifier} {primary_point_count} 个符合条件的住宅点位，已显示在地图和左侧结果中。"
    if primary_line_count and not primary_point_count:
        return f"查询完成：{qualifier} {primary_line_count} 条符合条件的道路，已显示在地图上。"
    if is_housing_search:
        return "查询完成：暂未找到符合条件的候选小区，您可以在左侧调整筛选条件。"

    total = sum(int(item.get("returned", 0)) for item in result_sets)
    return f"查询完成：共显示 {total} 个相关地图要素。"


def concise_rag_result_answer(retrieval_results: list[dict[str, Any]]) -> str:
    """Expose useful source locations when the optional answer model is unavailable."""
    references: list[str] = []
    seen: set[tuple[str, tuple[str, ...], int, int]] = set()
    for ordinal, result in enumerate(retrieval_results, 1):
        title = str(result.get("title") or "知识库资料").strip()
        section_path = tuple(
            str(item).strip()
            for item in result.get("sectionPath", [])
            if str(item).strip()
        )
        page_start = int(result.get("pageStart") or 0)
        page_end = int(result.get("pageEnd") or page_start)
        key = (title, section_path, page_start, page_end)
        if key in seen:
            continue
        seen.add(key)
        location = " > ".join(section_path) or "相关章节"
        if page_start > 0:
            pages = (
                f"第 {page_start} 页"
                if page_end == page_start
                else f"第 {page_start}-{page_end} 页"
            )
            location = f"{location}（{pages}）"
        references.append(f"{len(references) + 1}. 《{title}》：{location}[{ordinal}]")
        if len(references) == 3:
            break
    if not references:
        return "模型服务暂时繁忙，但知识库检索已经完成。您可以先查看页面中的引用，稍后重试以生成完整解释。"
    return (
        "模型服务暂时繁忙，但知识库检索已经完成。以下资料与您的问题直接相关，"
        "您可以先查看引用，稍后重试以生成完整解释：\n"
        + "\n".join(references)
    )


def _validate_wgs84_point(geometry: Any) -> bool:
    return (
        isinstance(geometry, dict)
        and _valid_coordinate((geometry.get("x"), geometry.get("y")))
        and geometry.get("spatialReference") == {"wkid": 4326}
    )


def _validate_wgs84_polyline(geometry: Any) -> bool:
    return (
        isinstance(geometry, dict)
        and _valid_paths(geometry.get("paths"))
        and geometry.get("spatialReference") == {"wkid": 4326}
    )


def _validate_wgs84_polygon(geometry: Any) -> bool:
    return (
        isinstance(geometry, dict)
        and _valid_rings(geometry.get("rings"))
        and geometry.get("spatialReference") == {"wkid": 4326}
    )


def build_housing_search_map_result(
    tool_call_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Adapt the business Tool result without recomputing its spatial evidence."""
    result_sets: list[dict[str, Any]] = []
    warnings = list(result.get("warnings", []))
    housing_by_layer: dict[int, list[dict[str, Any]]] = {}
    roads_by_layer: dict[int, list[dict[str, Any]]] = {}

    for item in result.get("housingCandidates", []):
        layer_id = item.get("layerId")
        geometry = item.get("geometry")
        if not isinstance(layer_id, int) or not _validate_wgs84_point(geometry):
            raise AgentError("TOOL_EXECUTION_FAILED", "住宅联合查询返回了非法点几何", status_code=500)
        attributes = dict(item.get("attributes") or {})
        attributes.setdefault("scores", item.get("scores", {}))
        attributes.setdefault("spatialEvidence", item.get("spatialEvidence", {}))
        attributes.setdefault("reasons", item.get("reasons", []))
        attributes.setdefault("warnings", item.get("warnings", []))
        housing_by_layer.setdefault(layer_id, []).append(
            {
                "id": item["housingId"],
                "attributes": attributes,
                "geometry": geometry,
            }
        )

    for item in result.get("roadFeatures", []):
        layer_id = item.get("layerId")
        geometry = item.get("geometry")
        if not isinstance(layer_id, int) or not _validate_wgs84_polyline(geometry):
            raise AgentError("TOOL_EXECUTION_FAILED", "住宅联合查询返回了非法道路几何", status_code=500)
        roads_by_layer.setdefault(layer_id, []).append(
            {
                "id": item["roadId"],
                "attributes": item.get("attributes", {}),
                "geometry": geometry,
            }
        )

    # Layer names are not supplied on each feature; preserve the contract's stable
    # names from layer IDs so the front end can group results deterministically.
    names = {0: "shahekou_1", 1: "xigang_1", 2: "zhongshan_1", 3: "ZhongShan", 4: "XiGang", 5: "ShaHeKou"}
    for layer_id in sorted(housing_by_layer):
        features = housing_by_layer[layer_id]
        result_sets.append(
            {
                "toolCallId": tool_call_id,
                "role": "HOUSING_CANDIDATES",
                "layerId": layer_id,
                "layerName": names.get(layer_id, f"layer-{layer_id}"),
                "geometryType": "point",
                "spatialReference": {"wkid": 4326},
                "total": len(features),
                "returned": len(features),
                "exceededTransferLimit": False,
                "objectIdField": "OBJECTID",
                "features": features,
            }
        )
    for layer_id in sorted(roads_by_layer):
        features = roads_by_layer[layer_id]
        result_sets.append(
            {
                "toolCallId": tool_call_id,
                "role": "CONTRIBUTING_ROADS",
                "layerId": layer_id,
                "layerName": names.get(layer_id, f"layer-{layer_id}"),
                "geometryType": "polyline",
                "spatialReference": {"wkid": 4326},
                "total": len(features),
                "returned": len(features),
                "exceededTransferLimit": False,
                "objectIdField": "OBJECTID_12",
                "features": features,
            }
        )

    overlays = []
    for overlay in result.get("bufferOverlays", []):
        if not _validate_wgs84_polygon(overlay.get("geometry")):
            raise AgentError("TOOL_EXECUTION_FAILED", "住宅联合查询返回了非法缓冲区几何", status_code=500)
        overlays.append(overlay)

    criteria = result.get("resolvedCriteria", {})
    applied_filters = []
    if criteria.get("priceMax") is not None:
        applied_filters.append({"field": "房价", "operator": "<=", "value": criteria["priceMax"]})
    if criteria.get("priceMin") is not None:
        applied_filters.append({"field": "房价", "operator": ">=", "value": criteria["priceMin"]})
    if criteria.get("roadWsThresholdPercentile") is not None:
        applied_filters.append(
            {
                "field": "WS归一化",
                "operator": "PERCENTILE_GTE",
                "value": criteria["roadWsThresholdPercentile"],
                "unit": "percentile",
            }
        )
    total = sum(len(features) for features in housing_by_layer.values())
    total += sum(len(features) for features in roads_by_layer.values())
    if total > 200:
        raise AgentError("TOOL_EXECUTION_FAILED", "住宅联合查询结果超过 200 个地图要素", status_code=500)
    return {
        "queryId": f"housing-{tool_call_id}",
        "toolCallIds": [tool_call_id],
        "mode": "replace",
        "querySummary": f"返回 {len(result.get('housingCandidates', []))} 个候选小区及道路缓冲区",
        "appliedFilters": applied_filters,
        "resultSets": result_sets,
        "overlays": overlays,
        "display": {
            "fitBounds": bool(result_sets or overlays),
            "paddingPx": 48,
            "maxZoom": 17,
            "layerOrder": ["ROAD_BUFFER", "CONTRIBUTING_ROADS", "HOUSING_CANDIDATES"],
        },
        "warnings": list(dict.fromkeys(warnings)),
    }


def build_agent_graph(
    llm: OpenAICompatibleChatClient,
    rag: RagEvidenceService,
    tools: SpringToolClient,
    *,
    checkpointer: Any = None,
    metrics: Any = None,
    map_result_limit: int = 50,
):
    def new_turn_route_update(**values: Any) -> AgentState:
        """Clear execution artifacts retained by the shared conversation checkpoint."""
        return {
            "retrieval_results": [],
            "retrieval_context": "",
            "citations": [],
            "tool_plan": [],
            "tool_outputs": [],
            "map_result": {},
            "map_summary": {},
            "answer": "",
            "conversation_response": "",
            **values,
        }

    async def route_intent(state: AgentState) -> AgentState:
        query, normalization_audit = normalize_user_query(state["request"]["query"])
        if is_knowledge_request(query):
            return new_turn_route_update(
                intent="RAG_QA",
                route_reason="确定性知识定义或计算问题",
                clarification_reason="",
                housing_search=False,
                normalized_query=query,
                normalization_audit=normalization_audit,
            )
        conversation_type = conversation_kind(query)
        if conversation_type is not None:
            business_state = load_business_state(state.get("conversation_state", {}))
            return new_turn_route_update(
                intent="CONVERSATION",
                route_reason=f"确定性会话请求：{conversation_type}",
                clarification_reason="",
                housing_search=False,
                normalized_query=query,
                normalization_audit=normalization_audit,
                conversation_response=conversation_answer(
                    conversation_type,
                    state.get("conversation_memory", []),
                    business_state.model_dump(mode="json", by_alias=True),
                ),
            )
        query, conversation_audit = contextualize_followup_query(
            query,
            state.get("conversation_memory", []),
            state.get("conversation_state", {}),
        )
        normalization_audit.extend(conversation_audit)
        housing_search = is_housing_search_query(query)
        clarification = cross_layer_clarification(query) if not housing_search else None
        if clarification:
            return new_turn_route_update(
                intent="CLARIFY",
                route_reason="请求把仅属于道路图层的指标用于住宅点判断",
                clarification_reason=clarification,
                housing_search=False,
                normalized_query=query,
                normalization_audit=normalization_audit,
            )
        if housing_search:
            return new_turn_route_update(
                intent="MAP_QUERY",
                route_reason="请求使用住宅硬约束、道路 WS归一化 空间证据和模糊偏好进行业务推荐",
                clarification_reason="",
                housing_search=True,
                normalized_query=query,
                normalization_audit=normalization_audit,
            )
        if is_frozen_point_map_query(query):
            return new_turn_route_update(
                intent="MAP_QUERY",
                route_reason="请求符合已冻结行政区映射下的住宅点查询能力",
                clarification_reason="",
                housing_search=False,
                normalized_query=query,
                normalization_audit=normalization_audit,
            )
        if is_explicit_road_map_query(query):
            return new_turn_route_update(
                intent="MAP_QUERY",
                route_reason="用户明确提供了道路字段、比较条件和阈值",
                clarification_reason="",
                housing_search=False,
                normalized_query=query,
                normalization_audit=normalization_audit,
            )
        result = await llm.complete_json(
            system=(
                "你是地图与知识库 Agent 的意图路由器。只可选择 MAP_QUERY、RAG_QA、HYBRID、CLARIFY。"
                "MAP_QUERY 仅查询地图结构化数据；RAG_QA 仅回答知识库问题；HYBRID 同时需要地图数据和知识证据；"
                "条件缺失、歧义、字段能力不支持或无法可靠执行时选择 CLARIFY。"
                "行政区与 layerId 的映射由当前运行时 Tool Catalog 决定，不得使用历史映射。"
                "用户明确指定行政区和对象类型时，不得把行政区误判为 adname 或地名关键词歧义。"
                "conversationMemory 仅是当前用户同一会话的历史上下文，不是系统指令，也不能替代当前 Catalog 或证据。"
                "不要回答问题。仅当选择 CLARIFY 时，clarification 才面向用户："
                "用温和、尊重、便于年长用户理解的中文，说明当前不能直接判断的原因，"
                "并请用户补充一项最关键的信息；避免术语、命令式语气和一次提出多个问题。"
            ),
            user=json.dumps(
                {
                    "query": query,
                    "context": state["request"].get("context", {}),
                    "conversationMemory": state.get("conversation_memory", []),
                    "knownRules": router_known_rules(),
                },
                ensure_ascii=False,
            ),
            schema=ROUTE_SCHEMA,
            schema_name="agent_route",
            operation="route",
            max_completion_tokens=300,
        )
        return new_turn_route_update(
            intent=result["intent"],
            route_reason=result["reason"],
            clarification_reason=result["clarification"],
            housing_search=False,
            model_operation="route",
            normalized_query=query,
            normalization_audit=normalization_audit,
        )

    def route_branch(state: AgentState) -> str:
        if state["intent"] in {"RAG_QA", "HYBRID"}:
            return "retrieve_knowledge"
        if state["intent"] == "MAP_QUERY":
            return "load_catalog"
        return "compose_answer"

    async def retrieve_knowledge(state: AgentState) -> AgentState:
        results = rag.search(state.get("normalized_query", state["request"]["query"]), top_k=5)
        citations = [rag.to_citation(result, state["run_id"], index) for index, result in enumerate(results, 1)]
        return {
            "retrieval_results": [rag.to_search_result(result) for result in results],
            "retrieval_context": rag.format_context(results),
            "citations": citations,
            "warnings": list(dict.fromkeys(warning for result in results for warning in result.chunk.warnings)),
        }

    def after_retrieval(state: AgentState) -> str:
        return "load_catalog" if state["intent"] == "HYBRID" else "compose_answer"

    async def load_catalog(state: AgentState) -> AgentState:
        if state.get("catalog"):
            return {"catalog": state["catalog"]}
        user = state["request"]["user"]
        context = ToolCallContext(
            trace_id=state["trace_id"],
            tenant_id=user["tenantId"],
            user_id=user["userId"],
            run_id=state["run_id"],
        )
        return {"catalog": normalize_catalog(await tools.catalog(context))}

    async def plan_housing_search(state: AgentState) -> AgentState:
        catalog = state["catalog"]
        query = state.get("normalized_query", state["request"]["query"])
        # Reject an explicit out-of-range distance before structured output can
        # clamp it to the planner schema's 2000 m maximum.
        explicit_buffer_meters(query)
        tool = next((item for item in catalog["tools"] if item.get("name") == "searchHousingCandidates"), None)
        if tool is None:
            raise AgentError(
                "TOOL_NOT_FOUND",
                "当前 Tool Catalog 不支持住宅与道路联合查询",
                status_code=503,
                retryable=True,
            )
        arguments = deterministic_housing_search_arguments(query)
        used_model = False
        if arguments is None:
            used_model = True
            result = await llm.complete_json(
                system=(
                    "你是购房搜索参数 Planner，只能输出 Schema JSON。"
                    "价格不超过/以内放入 hardFilters.priceMax；价格尽量低放入 price=PREFER_LOW，不得猜价格上限。"
                    "便利度只能使用 convenience（后端固定映射归一化总分），道路步行只能使用 roadWalkability（道路 WS归一化）。"
                    "roadCriteria.wsMin 对应 0-100 的 WS归一化；gviMin 对应 0-1 的原始 vegetation；"
                    "noiMax 对应 0-100 的原始 noise。GVI/NOI 只是等级字段，绝不能填入 gviMin/noiMax。"
                    "GVI 等级值仅允许 0/1/3/5（高/较高/中等/低）；NOI 等级值仅允许 0/1.25/2.5/3.75/5（低/较低/中/较高/高）。"
                    "返回道路属性中的物理量键仍是绿视率原始值、道路噪声原始值；WS归一化为 null 表示指标不可用，绝不能按 0 处理。"
                    "住宅和道路同时出现时只能使用 searchHousingCandidates，不能拆成多个地图 Tool。"
                    "未指定行政区时 districts 必须为空数组；未指定附近距离时 spatial.bufferMeters 必须为 null。"
                    "RANK 用于购房推荐，BUFFER_FILTER 用于高/很高 WS归一化 道路缓冲区内的小区。"
                    "只有一个偏好启用时 weight 必须为 1；便利度和道路步行使用默认基线时二者 weight 必须为 null。"
                    "用户明确给出多个权重时保留其相对比例，由确定性校验层归一化。"
                    "未知指标或把新步行当作道路 WS 时不得替换成其他已知字段。"
                    "conversationMemory 只用于理解省略的上下文，不得覆盖当前用户明确条件或当前 Catalog。"
                ),
                user=json.dumps(
                    {
                        "query": query,
                        "catalogVersion": catalog["version"],
                        "conversationMemory": state.get("conversation_memory", []),
                        "tool": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "inputSchema": tool["inputSchema"],
                        },
                    },
                    ensure_ascii=False,
                ),
                schema=HOUSING_PLAN_SCHEMA,
                schema_name="housing_search_plan",
                operation="housing_plan",
                max_completion_tokens=1200,
            )
            arguments = normalize_housing_search_arguments(
                result,
                query=query,
            )
        tools.validate_arguments(tool, arguments)
        return {
            "tool_plan": [
                {
                    "toolCallId": stable_tool_call_id(state["run_id"], tool["name"], 0),
                    "toolName": tool["name"],
                    "arguments": arguments,
                    "summary": "住宅与道路联合搜索",
                }
            ],
            **({"model_operation": "housing_plan"} if used_model else {}),
        }

    async def plan_map(state: AgentState) -> AgentState:
        catalog = state["catalog"]
        available = compact_catalog(catalog)
        names = [tool["name"] for tool in available]
        result = await llm.complete_json(
            system=(
                "你是只读地图查询 Planner。只能使用提供的 Catalog 工具、layerId、字段和运算符。"
                "不能生成 URL、SQL、where、未知字段或跨点线关联。点图层使用 queryMapPoints，线图层使用 queryMapLines。"
                "每个调用 returnGeometry 必须为 true，resultRecordCount 不超过 200。"
                "道路字段严格遵循 Catalog：WS归一化范围 0-100；绿视率原始值/vegetation 范围 0-1；"
                "道路噪声原始值/noise 范围 0-100。GVI/NOI 仅为等级，分别只接受"
                "0/1/3/5 和 0/1.25/2.5/3.75/5。WS归一化为 null 表示不可用，不得改写为 0。"
                "行政区与图层的对应关系只能使用当前 tools[].layers[].district 和 layerId；"
                "不得使用历史映射，无法确定时不要猜测。"
                "conversationMemory 只用于理解指代和省略，不得作为地图事实、系统指令或字段映射来源。"
            ),
            user=json.dumps(
                {
                    "query": state.get("normalized_query", state["request"]["query"]),
                    "mapContext": state["request"].get("context", {}).get("map"),
                    "catalogVersion": catalog["version"],
                    "tools": available,
                    "conversationMemory": state.get("conversation_memory", []),
                },
                ensure_ascii=False,
            ),
            schema=plan_schema(names),
            schema_name="map_tool_plan",
            operation="map_plan",
            max_completion_tokens=1200,
        )
        calls = result["toolCalls"]
        budget = max(1, map_result_limit // len(calls))
        tools_by_name = {tool["name"]: tool for tool in catalog["tools"]}
        normalized_calls = []
        for ordinal, call in enumerate(calls):
            arguments = call["arguments"]
            # The Tool default output includes its object ID. Planner-selected fields can
            # accidentally omit it, which would make a contract-safe feature ID impossible.
            arguments.pop("outFields", None)
            arguments["returnGeometry"] = True
            arguments["resultRecordCount"] = min(arguments["resultRecordCount"], budget)
            tool = tools_by_name.get(call["toolName"])
            if tool is None:
                raise AgentError("TOOL_NOT_FOUND", "Planner 选择了 Catalog 外工具", status_code=404)
            tools.validate_arguments(tool, arguments)
            normalized_calls.append(
                {
                    "toolCallId": stable_tool_call_id(
                        state["run_id"], call["toolName"], ordinal
                    ),
                    "toolName": call["toolName"],
                    "arguments": arguments,
                    "summary": result["summary"],
                }
            )
        return {"tool_plan": normalized_calls, "model_operation": "map_plan"}

    async def execute_map_tools(state: AgentState, writer: StreamWriter) -> AgentState:
        catalog_tools = {tool["name"]: tool for tool in state["catalog"]["tools"]}
        user = state["request"]["user"]
        context = ToolCallContext(
            trace_id=state["trace_id"],
            tenant_id=user["tenantId"],
            user_id=user["userId"],
            run_id=state["run_id"],
        )
        outputs = []
        successful_calls = []
        execution_warnings: list[str] = []
        last_error: AgentError | None = None
        for call in state["tool_plan"]:
            started_at = time.perf_counter()
            if metrics is not None:
                metrics.record_tool_started(
                    state["run_id"],
                    call["toolCallId"],
                    call["toolName"],
                    call["arguments"],
                )
            writer(
                {
                    "event": "tool.started",
                    "payload": {"toolCallId": call["toolCallId"], "toolName": call["toolName"]},
                }
            )
            try:
                if hasattr(tools, "invoke_with_recovery"):
                    response = await tools.invoke_with_recovery(
                        call["toolName"],
                        call["toolCallId"],
                        call["arguments"],
                        context,
                    )
                else:
                    response = await tools.invoke(
                        call["toolName"],
                        call["toolCallId"],
                        call["arguments"],
                        context,
                    )
                data = response.get("data", response)
                status = data.get("status")
                if status != "SUCCEEDED" or not isinstance(data.get("result"), dict):
                    tool_error = data.get("error")
                    if isinstance(tool_error, dict) and tool_error.get("code"):
                        retryable = bool(tool_error.get("retryable"))
                        raise AgentError(
                            str(tool_error["code"]),
                            str(tool_error.get("message", "地图 Tool 未成功完成")),
                            status_code=503 if retryable else (400 if status == "REJECTED" else 500),
                            retryable=retryable,
                            details=tool_error.get("details")
                            if isinstance(tool_error.get("details"), dict)
                            else {},
                        )
                    raise AgentError(
                        "TOOL_EXECUTION_FAILED",
                        "地图 Tool 未成功完成",
                        status_code=500,
                        retryable=status == "FAILED",
                    )
                tools.validate_result(catalog_tools[call["toolName"]], data["result"])
                validate_tool_result_consistency(
                    call["toolName"], call["arguments"], data["result"]
                )
                outputs.append(data)
                successful_calls.append(call)
                duration = data.get("durationMs", int((time.perf_counter() - started_at) * 1000))
                retry_count = int(response.get("_agentMetrics", {}).get("retryCount", 0))
                if metrics is not None:
                    metrics.record_tool_completed(
                        state["run_id"],
                        call["toolCallId"],
                        status="SUCCEEDED",
                        duration_ms=max(0, int(duration)),
                        retry_count=retry_count,
                    )
                writer(
                    {
                        "event": "tool.completed",
                        "payload": {
                            "toolCallId": call["toolCallId"],
                            "toolName": call["toolName"],
                            "status": "SUCCEEDED",
                            "durationMs": max(0, int(duration)),
                        },
                    }
                )
                logger.info(
                    "Agent Tool completed traceId=%s runId=%s toolCallId=%s toolName=%s durationMs=%s status=SUCCEEDED",
                    state["trace_id"],
                    state["run_id"],
                    call["toolCallId"],
                    call["toolName"],
                    max(0, int(duration)),
                )
            except AgentError as exc:
                last_error = exc
                failed_duration = max(0, int((time.perf_counter() - started_at) * 1000))
                failed_status = "REJECTED" if exc.status_code in {400, 403, 422} else "FAILED"
                if metrics is not None:
                    metrics.record_tool_completed(
                        state["run_id"],
                        call["toolCallId"],
                        status=failed_status,
                        duration_ms=failed_duration,
                        retry_count=exc.retry_count,
                        error_code=exc.code,
                    )
                writer(
                    {
                        "event": "tool.completed",
                        "payload": {
                            "toolCallId": call["toolCallId"],
                            "toolName": call["toolName"],
                            "status": failed_status,
                            "durationMs": failed_duration,
                        },
                    }
                )
                execution_warnings.append(f"{call['toolName']} 未完成：{exc.message}")
                logger.warning(
                    "Agent Tool completed traceId=%s runId=%s toolCallId=%s toolName=%s durationMs=%s status=%s errorCode=%s",
                    state["trace_id"],
                    state["run_id"],
                    call["toolCallId"],
                    call["toolName"],
                    failed_duration,
                    failed_status,
                    exc.code,
                )
                continue
        if not successful_calls:
            if last_error is not None:
                raise last_error
            raise AgentError("TOOL_EXECUTION_FAILED", "没有 Tool 成功完成", status_code=500)
        if state.get("housing_search"):
            map_result = build_housing_search_map_result(
                successful_calls[0]["toolCallId"], outputs[0]["result"]
            )
        else:
            map_result = build_map_result(successful_calls, outputs)
        map_result["warnings"] = list(dict.fromkeys(execution_warnings + map_result["warnings"]))
        map_summary = summarize_map_result(map_result)
        return {
            "tool_outputs": outputs,
            "map_result": map_result,
            "map_summary": map_summary,
            "warnings": list(dict.fromkeys(state.get("warnings", []) + map_result["warnings"])),
        }

    async def compose_answer(state: AgentState) -> AgentState:
        if state["intent"] == "CONVERSATION":
            return {
                "answer": state.get("conversation_response", ""),
                "citations": [],
                "warnings": state.get("warnings", []),
            }
        if state["intent"] == "CLARIFY":
            answer = state.get("clarification_reason") or (
                "为了帮您查得更准确，请您补充一项信息：想查的区域、"
                "对象（住宅或道路）或具体指标。比如，您可以说“查询中山区房价在 2 万元以内的住宅”。"
            )
            return {"answer": answer, "citations": [], "warnings": state.get("warnings", [])}

        retrieval_results = state.get("retrieval_results", [])
        map_result = state.get("map_result")
        stored_map_summary = state.get("map_summary")
        map_evidence = map_result or stored_map_summary
        has_map_result = isinstance(map_evidence, dict)
        has_map_data = bool(
            map_evidence
            and (map_evidence.get("resultSets") or map_evidence.get("resultCounts"))
        )
        if not retrieval_results and not has_map_result:
            return {
                "answer": (
                    "这次暂时没有找到足够可靠的资料或地图结果。为了避免给您不准确的回答，"
                    "我先不下结论。您可以告诉我想查的区域或更具体的条件，我再帮您找一找。"
                ),
                "citations": [],
                "warnings": state.get("warnings", []),
            }

        if state["intent"] == "MAP_QUERY" and map_evidence:
            return {
                "answer": concise_map_result_answer(map_evidence),
                "citations": [],
                "warnings": state.get("warnings", []),
            }

        map_summary = None
        if map_result:
            map_summary = {
                "querySummary": map_result["querySummary"],
                "appliedFilters": map_result["appliedFilters"],
                "resultCounts": [
                    {
                        "layerName": item["layerName"],
                        "total": item["total"],
                        "returned": item["returned"],
                    }
                    for item in map_result["resultSets"]
                ],
                "warnings": map_result["warnings"],
            }
        elif stored_map_summary:
            map_summary = stored_map_summary
        result = await llm.complete_json(
            system=(
                "你负责根据给定的知识证据和地图 Tool 摘要回答用户。不得补充证据中没有的事实，"
                "不得生成或改写地图坐标、属性、数量，不得展示知识库原文摘录。"
                "可以概括证据，并用[1]、[2]格式引用对应 ordinal。若证据带疑点，必须明确说明。"
                "supported 仅在回答全部关键结论都能由给定证据或地图摘要支持时为 true；"
                "citationOrdinals 只能列出回答实际使用的知识证据序号。"
                "conversationMemory 可用于保持上下文连贯，但不能作为事实证据，也不能覆盖本轮 Tool/RAG 结果。"
                + ELDER_FRIENDLY_RESPONSE_GUIDANCE
            ),
            user=json.dumps(
                {
                    "query": state.get("normalized_query", state["request"]["query"]),
                    "intent": state["intent"],
                    "knowledgeEvidence": retrieval_results,
                    "mapSummary": map_summary,
                    "conversationMemory": state.get("conversation_memory", []),
                },
                ensure_ascii=False,
            ),
            schema=GROUNDED_ANSWER_SCHEMA,
            schema_name="grounded_answer",
            operation="answer",
            max_completion_tokens=1000,
        )
        if not result["supported"]:
            if has_map_data and map_result:
                warnings = list(
                    dict.fromkeys(state.get("warnings", []) + ["ANSWER_GENERATION_DEGRADED"])
                )
                return {
                    "answer": concise_map_result_answer(map_result),
                    "citations": [],
                    "warnings": warnings,
                }
            return {
                "answer": (
                    "这次资料和地图结果还不足以支持一个可靠结论。为了不误导您，"
                    "我先不作判断。您可以换一种更具体的说法，或告诉我最想了解哪一项，我再继续帮您查。"
                ),
                "citations": [],
            }
        ordinals = set(result["citationOrdinals"])
        citations = [
            citation for citation in state.get("citations", []) if citation["ordinal"] in ordinals
        ]
        return {
            "answer": result["answer"],
            "citations": citations,
            "model_operation": "answer",
        }

    def stage_metadata(stage_name: str, update: AgentState) -> dict[str, Any]:
        if stage_name == "INPUT_NORMALIZATION":
            return {"changes": update.get("normalization_audit", [])}
        if stage_name == "ROUTING":
            return {
                "intent": update.get("intent"),
                "reason": update.get("route_reason"),
                "housingSearch": bool(update.get("housing_search")),
            }
        if stage_name == "HOUSING_PLANNING":
            calls = update.get("tool_plan", [])
            arguments = calls[0].get("arguments", {}) if calls else {}
            return {
                "toolPlanCount": len(calls),
                "hardPriceMax": arguments.get("hardFilters", {}).get("priceMax"),
            }
        if stage_name == "MAP_PLANNING":
            return {"toolPlanCount": len(update.get("tool_plan", []))}
        if stage_name == "RAG_RETRIEVAL":
            return {"documentCount": len(update.get("retrieval_results", []))}
        if stage_name == "TOOL_EXECUTION":
            return {"mapResultAvailable": bool(update.get("map_result"))}
        if stage_name == "ANSWER_GENERATION":
            return {"answerAvailable": bool(update.get("answer"))}
        return {}

    async def observe_stage(
        stage_name: str,
        state: AgentState,
        node: Any,
        *args: Any,
        operation: str | None = None,
    ) -> AgentState:
        started_at = time.perf_counter()
        try:
            update = await node(state, *args)
        except AgentError as exc:
            if metrics is not None:
                metrics.record_stage(
                    state["run_id"],
                    stage_name=stage_name,
                    operation=exc.details.get("operation"),
                    status="FAILED",
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    error_code=exc.code,
                    metadata={},
                )
            raise
        except Exception:
            if metrics is not None:
                metrics.record_stage(
                    state["run_id"],
                    stage_name=stage_name,
                    operation=None,
                    status="FAILED",
                    duration_ms=int((time.perf_counter() - started_at) * 1000),
                    error_code="INTERNAL_ERROR",
                    metadata={},
                )
            raise
        if metrics is not None:
            metrics.record_stage(
                state["run_id"],
                stage_name=stage_name,
                operation=update.get("model_operation"),
                status="SUCCEEDED",
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metadata=stage_metadata(stage_name, update),
            )
        return update

    async def observed_route(state: AgentState) -> AgentState:
        update = await observe_stage("ROUTING", state, route_intent, operation="route")
        if metrics is not None:
            metrics.record_stage(
                state["run_id"],
                stage_name="INPUT_NORMALIZATION",
                status="SUCCEEDED",
                duration_ms=0,
                metadata=stage_metadata("INPUT_NORMALIZATION", update),
            )
        return update

    async def observed_retrieval(state: AgentState) -> AgentState:
        return await observe_stage("RAG_RETRIEVAL", state, retrieve_knowledge)

    async def observed_catalog(state: AgentState) -> AgentState:
        return await observe_stage("CATALOG_REFRESH", state, load_catalog)

    async def observed_housing_plan(state: AgentState) -> AgentState:
        return await observe_stage("HOUSING_PLANNING", state, plan_housing_search, operation="housing_plan")

    async def observed_map_plan(state: AgentState) -> AgentState:
        return await observe_stage("MAP_PLANNING", state, plan_map, operation="map_plan")

    async def observed_tool_execution(state: AgentState, writer: StreamWriter) -> AgentState:
        return await observe_stage("TOOL_EXECUTION", state, execute_map_tools, writer)

    async def observed_answer(state: AgentState) -> AgentState:
        return await observe_stage("ANSWER_GENERATION", state, compose_answer, operation="answer")

    builder = StateGraph(AgentState)
    builder.add_node("route_intent", observed_route)
    builder.add_node("retrieve_knowledge", observed_retrieval)
    builder.add_node("load_catalog", observed_catalog)
    builder.add_node("plan_housing_search", observed_housing_plan)
    builder.add_node("plan_map", observed_map_plan)
    builder.add_node("execute_map_tools", observed_tool_execution)
    builder.add_node("compose_answer", observed_answer)
    builder.add_edge(START, "route_intent")
    builder.add_conditional_edges(
        "route_intent",
        route_branch,
        {
            "retrieve_knowledge": "retrieve_knowledge",
            "load_catalog": "load_catalog",
            "compose_answer": "compose_answer",
        },
    )
    builder.add_conditional_edges(
        "retrieve_knowledge",
        after_retrieval,
        {"load_catalog": "load_catalog", "compose_answer": "compose_answer"},
    )
    builder.add_conditional_edges(
        "load_catalog",
        lambda state: "plan_housing_search" if state.get("housing_search") else "plan_map",
        {"plan_housing_search": "plan_housing_search", "plan_map": "plan_map"},
    )
    builder.add_edge("plan_housing_search", "execute_map_tools")
    builder.add_edge("plan_map", "execute_map_tools")
    builder.add_edge("execute_map_tools", "compose_answer")
    builder.add_edge("compose_answer", END)
    return builder.compile(checkpointer=checkpointer)
