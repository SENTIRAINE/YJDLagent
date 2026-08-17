from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.capabilities import (
    DISTRICT_NAMES,
    FOLLOWUP_REFERENCE_TERMS,
    HOUSING,
    entity_from_text,
    has_housing_preference,
)


class StateModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class EntityContext(StateModel):
    entity_type: Literal["HOUSING", "ROAD"] | None = Field(None, alias="entityType")
    districts: list[str] = Field(default_factory=list)
    business_object_ids: list[str] = Field(default_factory=list, alias="businessObjectIds")
    selected_layer_ids: list[int] = Field(default_factory=list, alias="selectedLayerIds")


class QueryContext(StateModel):
    last_successful_query: str | None = Field(None, alias="lastSuccessfulQuery")
    route: str | None = None
    tool_name: str | None = Field(None, alias="toolName")
    tool_arguments: dict[str, Any] = Field(default_factory=dict, alias="toolArguments")
    hard_filters: dict[str, Any] = Field(default_factory=dict, alias="hardFilters")
    preferences: dict[str, Any] = Field(default_factory=dict)
    result_ref: dict[str, Any] | None = Field(None, alias="resultRef")


class ConversationBusinessState(StateModel):
    schema_version: int = Field(1, alias="schemaVersion")
    entity_context: EntityContext = Field(default_factory=EntityContext, alias="entityContext")
    query_context: QueryContext = Field(default_factory=QueryContext, alias="queryContext")
    map_context: dict[str, Any] | None = Field(None, alias="mapContext")
    summary: str = ""
    history_digest: list[dict[str, str]] = Field(default_factory=list, alias="historyDigest")
    updated_at: str | None = Field(None, alias="updatedAt")


def load_business_state(wrapper: Any) -> ConversationBusinessState:
    payload = wrapper.get("state", {}) if isinstance(wrapper, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return ConversationBusinessState.model_validate(payload)


def _preference_clauses(preferences: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    mapping = {
        "price": "房价尽量低",
        "convenience": "社区便利度稍高",
        "roadWalkability": "道路步行条件稍高",
    }
    for name, clause in mapping.items():
        value = preferences.get(name)
        if isinstance(value, dict) and value.get("enabled"):
            clauses.append(clause)
    return clauses


def contextualize_with_state(
    query: str, state: ConversationBusinessState
) -> tuple[str, list[dict[str, str]]]:
    """Compose an elliptical follow-up from the last committed business query."""
    if entity_from_text(query) is not None:
        return query, []
    context = state.entity_context
    previous = state.query_context
    has_reference = any(term in query for term in FOLLOWUP_REFERENCE_TERMS)
    district_only = any(name in query for name in DISTRICT_NAMES)
    preference_only = has_housing_preference(query)
    if not (has_reference or district_only or preference_only):
        return query, []
    if context.entity_type is None or not previous.last_successful_query:
        return query, []

    clauses: list[str] = []
    if context.entity_type == "HOUSING":
        clauses.append(f"查询对象为{HOUSING.display_name}")
    else:
        clauses.append("查询对象为道路")
    if not any(name in query for name in DISTRICT_NAMES):
        clauses.extend(context.districts)
    price_max = previous.hard_filters.get("priceMax")
    if price_max is not None and "房价" not in query and "价格" not in query and "预算" not in query:
        clauses.append(f"房价不高于{price_max:g}" if isinstance(price_max, float) else f"房价不高于{price_max}")
    if not preference_only:
        clauses.extend(_preference_clauses(previous.preferences))
    if not clauses:
        return query, []
    composed = f"{query.rstrip('，,。！？!? ')}；沿用上一轮已提交条件：{'，'.join(clauses)}"
    return composed, [{"kind": "conversation_query_state_inheritance", "from": previous.last_successful_query, "to": composed}]


def _districts_from_query(query: str) -> list[str]:
    return [name for name in DISTRICT_NAMES if name in query]


def _map_result_reference(map_result: Any) -> dict[str, Any] | None:
    if not isinstance(map_result, dict):
        return None
    result_sets = map_result.get("resultSets") or map_result.get("resultCounts", [])
    return {
        "queryId": map_result.get("queryId"),
        "querySummary": map_result.get("querySummary"),
        "resultCounts": [
            {
                "role": item.get("role"),
                "layerId": item.get("layerId"),
                "total": item.get("total"),
                "returned": item.get("returned"),
            }
            for item in result_sets
        ],
    }


def build_committed_state(
    previous: ConversationBusinessState,
    *,
    query: str,
    answer: str,
    intent: str | None,
    tool_plan: list[dict[str, Any]],
    map_result: Any,
    request_context: dict[str, Any],
    updated_at: str,
) -> dict[str, Any]:
    state = previous.model_copy(deep=True)
    state.map_context = deepcopy(request_context.get("map"))
    state.summary = answer[:2000]
    state.updated_at = updated_at
    state.entity_context.business_object_ids = list(request_context.get("businessObjectIds", []))
    state.history_digest = (
        state.history_digest
        + [{"query": query[:500], "answer": answer[:800], "route": str(intent or "")}]
    )[-12:]

    # Only a successful map query may replace the reusable business query state.
    if intent != "MAP_QUERY" or not tool_plan:
        return state.model_dump(mode="json", by_alias=True)
    call = tool_plan[0]
    tool_name = str(call.get("toolName", ""))
    arguments = deepcopy(call.get("arguments", {}))
    entity_type: Literal["HOUSING", "ROAD"] | None = None
    districts = _districts_from_query(query)
    hard_filters: dict[str, Any] = {}
    preferences: dict[str, Any] = {}
    selected_layers: list[int] = []
    if tool_name == "searchHousingCandidates":
        entity_type = "HOUSING"
        districts = list(arguments.get("districts", districts))
        hard_filters = deepcopy(arguments.get("hardFilters", {}))
        preferences = deepcopy(arguments.get("preferences", {}))
    elif tool_name == "queryMapPoints":
        entity_type = "HOUSING"
        selected_layers = [arguments["layerId"]] if isinstance(arguments.get("layerId"), int) else []
        for item in arguments.get("filters", []):
            if item.get("field") == "房价" and item.get("operator") in {"<=", "<"}:
                hard_filters["priceMax"] = item.get("value")
        hard_filters["mapFilters"] = deepcopy(arguments.get("filters", []))
    elif tool_name == "queryMapLines":
        entity_type = "ROAD"
        selected_layers = [arguments["layerId"]] if isinstance(arguments.get("layerId"), int) else []
        hard_filters["mapFilters"] = deepcopy(arguments.get("filters", []))
    if entity_type is None:
        return state.model_dump(mode="json", by_alias=True)

    state.entity_context = EntityContext(
        entityType=entity_type,
        districts=districts,
        businessObjectIds=request_context.get("businessObjectIds", []),
        selectedLayerIds=selected_layers,
    )
    state.query_context = QueryContext(
        lastSuccessfulQuery=query,
        route=intent,
        toolName=tool_name,
        toolArguments=arguments,
        hardFilters=hard_filters,
        preferences=preferences,
        resultRef=_map_result_reference(map_result),
    )
    return state.model_dump(mode="json", by_alias=True)
