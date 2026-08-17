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
    filter_fields=("name", "GVI", "NOI", "WS"),
    preference_aliases={},
)

ROAD_ONLY_METRIC_TERMS = ("GVI", "NOI", "WS", "绿视率", "道路噪声", "步行指数")
DISTRICT_NAMES = ("中山区", "西岗区", "沙河口区")
FOLLOWUP_REFERENCE_TERMS = ("这里面", "这些", "其中", "从中", "刚才", "上次", "上一轮", "前面的结果")


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


def router_known_rules() -> list[str]:
    housing_fields = "、".join(HOUSING.filter_fields)
    road_fields = "、".join(ROAD.filter_fields)
    return [
        "GVI、NOI、WS 只属于道路图层",
        f"住宅点可筛选 {housing_fields}",
        "住宅推荐支持价格偏好、社区便利度和道路步行偏好；社区便利度映射到 convenience，不是未知字段",
        f"道路可筛选 {road_fields}",
        "用户明确提供道路字段、运算符和阈值时执行 MAP_QUERY，不自行解释单位或推荐阈值",
        "行政区与 layerId 的映射必须使用当前运行时 Tool Catalog",
    ]

