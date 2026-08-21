from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


EntityType = Literal["HOUSING", "ROAD"]


@dataclass(frozen=True)
class BusinessCapability:
    entity_type: EntityType
    display_name: str
    aliases: tuple[str, ...]
    filter_fields: tuple[str, ...]
    preference_aliases: dict[str, tuple[str, ...]]


HOUSING = BusinessCapability(
    entity_type="HOUSING",
    display_name="住宅小区",
    aliases=("住宅", "住房", "房子", "房屋", "小区", "楼盘", "居住项目", "挑房", "买房", "购房"),
    filter_fields=("name", "adname", "房价", "覆盖度评分"),
    preference_aliases={
        "price": ("价格尽量低", "房价尽量低", "便宜一点", "便宜点", "预算友好"),
        "convenience": ("便利", "便利度", "方便", "生活方便", "社区便利度", "省心"),
        "roadWalkability": ("道路步行", "步行条件", "周边道路", "好走", "走路", "步行指数"),
    },
)

ROAD = BusinessCapability(
    entity_type="ROAD",
    display_name="道路",
    aliases=("道路", "路段", "街道", "公路"),
    filter_fields=(
        "name",
        "GVI",
        "NOI",
        "WS归一化",
        "绿视率原始值",
        "道路噪声原始值",
    ),
    preference_aliases={},
)

ROAD_ONLY_METRIC_TERMS = (
    "GVI",
    "NOI",
    "WS",
    "WS归一化",
    "vegetation",
    "noise",
    "绿视率",
    "绿视率原始值",
    "道路噪声",
    "道路噪声原始值",
    "步行指数",
)
DISTRICT_NAMES = ("中山区", "西岗区", "沙河口区")
FOLLOWUP_REFERENCE_TERMS = (
    "这里面",
    "这些",
    "其中",
    "从中",
    "刚才",
    "上次",
    "上一轮",
    "前面的结果",
    "其他条件不变",
    "其余条件不变",
)
KNOWLEDGE_REQUEST_TERMS = (
    "知识库",
    "解释",
    "说明",
    "为什么",
    "如何",
    "怎么",
    "怎样",
    "怎么算",
    "依据",
    "定义",
    "是什么",
    "什么意思",
    "何谓",
    "计算方式",
    "计算方法",
    "公式",
    "含义",
    "口径",
    "指标介绍",
)
KNOWLEDGE_MAP_ACTION_TERMS = (
    "筛选",
    "查找",
    "找出",
    "显示",
    "定位",
    "推荐",
    "挑选",
    "选出",
)


def entity_from_text(text: str) -> EntityType | None:
    normalized = text.lower()
    has_housing = any(alias.lower() in normalized for alias in HOUSING.aliases)
    has_road = any(alias.lower() in normalized for alias in ROAD.aliases)
    if has_housing == has_road:
        return None
    return "HOUSING" if has_housing else "ROAD"


def has_housing_preference(text: str) -> bool:
    normalized = text.lower()
    return any(
        alias.lower() in normalized
        for aliases in HOUSING.preference_aliases.values()
        for alias in aliases
    )


def is_knowledge_request(text: str) -> bool:
    """Recognize explanatory questions without swallowing map operations."""
    normalized = text.lower()
    if not any(term.lower() in normalized for term in KNOWLEDGE_REQUEST_TERMS):
        return False
    return not any(term.lower() in normalized for term in KNOWLEDGE_MAP_ACTION_TERMS)


def router_known_rules() -> list[str]:
    housing_fields = "、".join(HOUSING.filter_fields)
    road_fields = "、".join(ROAD.filter_fields)
    return [
        "道路字段中，GVI/NOI 仅为等级；WS归一化为 0-100；绿视率原始值和道路噪声原始值分别对应 vegetation/noise",
        f"住宅点可筛选 {housing_fields}",
        "住宅推荐支持价格偏好、社区便利度和道路步行偏好；社区便利度映射到 convenience，不是未知字段",
        f"道路可筛选 {road_fields}",
        "用户明确提供道路字段、运算符和阈值时执行 MAP_QUERY，不自行解释单位或推荐阈值",
        "行政区与 layerId 的映射必须使用当前运行时 Tool Catalog",
    ]
