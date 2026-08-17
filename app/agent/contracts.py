from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Extent4326(ContractModel):
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    wkid: Literal[4326]

    @model_validator(mode="after")
    def validate_bounds(self) -> "Extent4326":
        if self.xmin >= self.xmax or self.ymin >= self.ymax:
            raise ValueError("extent minimums must be lower than maximums")
        if not (-180 <= self.xmin <= 180 and -180 <= self.xmax <= 180):
            raise ValueError("extent longitude must be WGS84")
        if not (-90 <= self.ymin <= 90 and -90 <= self.ymax <= 90):
            raise ValueError("extent latitude must be WGS84")
        return self


class MapContext(ContractModel):
    visible_layer_ids: list[int] = Field(alias="visibleLayerIds", max_length=6)
    zoom: float | None = Field(default=None, ge=0, le=19)
    extent: Extent4326 | None = None

    @model_validator(mode="after")
    def validate_layers(self) -> "MapContext":
        if len(set(self.visible_layer_ids)) != len(self.visible_layer_ids):
            raise ValueError("visibleLayerIds must be unique")
        if any(layer_id < 0 or layer_id > 5 for layer_id in self.visible_layer_ids):
            raise ValueError("visibleLayerIds must be between 0 and 5")
        return self


class RunContext(ContractModel):
    locale: str = Field(max_length=32)
    map: MapContext | None = None
    business_object_ids: list[str] = Field(default_factory=list, alias="businessObjectIds", max_length=50)

    @model_validator(mode="after")
    def validate_business_objects(self) -> "RunContext":
        if len(set(self.business_object_ids)) != len(self.business_object_ids):
            raise ValueError("businessObjectIds must be unique")
        if any(not value or len(value) > 128 for value in self.business_object_ids):
            raise ValueError("businessObjectIds contains an invalid value")
        return self


class UserContext(ContractModel):
    user_id: str = Field(alias="userId", min_length=1, max_length=128)
    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=128)
    roles: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_roles(self) -> "UserContext":
        if len(set(self.roles)) != len(self.roles):
            raise ValueError("roles must be unique")
        if any(not role or len(role) > 64 for role in self.roles):
            raise ValueError("roles contains an invalid value")
        return self


class LangGraphRunRequest(ContractModel):
    conversation_id: UUID = Field(alias="conversationId")
    message_id: UUID = Field(alias="messageId")
    query: str = Field(min_length=1, max_length=4000)
    context: RunContext
    user: UserContext

    @model_validator(mode="after")
    def normalize_query(self) -> "LangGraphRunRequest":
        self.query = self.query.strip()
        if not self.query:
            raise ValueError("query must not be blank")
        return self


class CancelRequest(ContractModel):
    reason: str = Field(min_length=1, max_length=128)


class ContractError(ContractModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class CitationSource(ContractModel):
    resource_ref: str = Field(alias="resourceRef", min_length=1, max_length=256)


class Citation(ContractModel):
    citation_id: str = Field(alias="citationId")
    ordinal: int = Field(ge=1)
    document_id: str = Field(alias="documentId")
    document_version: str = Field(alias="documentVersion")
    title: str
    content_type: str = Field(alias="contentType")
    section_path: list[str] = Field(alias="sectionPath")
    page_start: int = Field(alias="pageStart", ge=1)
    page_end: int = Field(alias="pageEnd", ge=1)
    chunk_id: str = Field(alias="chunkId")
    excerpt: Literal[""] = ""
    excerpt_allowed: Literal[False] = Field(False, alias="excerptAllowed")
    score: float
    source: CitationSource
    warnings: list[str] = Field(default_factory=list)


class RagSearchFilters(ContractModel):
    document_ids: list[str] = Field(default_factory=list, alias="documentIds", max_length=100)
    content_types: list[str] = Field(default_factory=list, alias="contentTypes", max_length=20)


class RagSearchRequest(ContractModel):
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(alias="topK", ge=1, le=20)
    filters: RagSearchFilters


class RagSearchResult(ContractModel):
    content: str
    score: float
    document_id: str = Field(alias="documentId")
    document_version: str = Field(alias="documentVersion")
    title: str
    content_type: str = Field(alias="contentType")
    chunk_id: str = Field(alias="chunkId")
    section_path: list[str] = Field(alias="sectionPath")
    page_start: int = Field(alias="pageStart", ge=1)
    page_end: int = Field(alias="pageEnd", ge=1)
    resource_ref: str = Field(alias="resourceRef")
    warnings: list[str]


class SseEnvelope(ContractModel):
    schema_version: Literal["1.1"] = Field("1.1", alias="schemaVersion")
    run_id: UUID = Field(alias="runId")
    message_id: UUID = Field(alias="messageId")
    sequence: int = Field(ge=1)
    trace_id: str = Field(alias="traceId", min_length=1, max_length=128)
    timestamp: datetime
    payload: dict[str, Any]


RunStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
Intent = Literal["MAP_QUERY", "RAG_QA", "HYBRID", "CLARIFY", "CONVERSATION"]


class RunStatusData(ContractModel):
    run_id: UUID = Field(alias="runId")
    status: RunStatus
    last_sequence: int = Field(alias="lastSequence", ge=0)
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")
    completed: dict[str, Any] | None
    error: ContractError | None
