from __future__ import annotations

from typing import Any


def summarize_map_result(map_result: Any) -> dict[str, Any] | None:
    """Remove feature geometry and attributes from a map result for persistence."""
    if not isinstance(map_result, dict):
        return None
    result_sets = map_result.get("resultSets") or map_result.get("resultCounts", [])
    return {
        "queryId": map_result.get("queryId"),
        "querySummary": map_result.get("querySummary"),
        "appliedFilters": map_result.get("appliedFilters", []),
        "resultCounts": [
            {
                "role": item.get("role"),
                "layerId": item.get("layerId"),
                "layerName": item.get("layerName"),
                "geometryType": item.get("geometryType"),
                "total": item.get("total", 0),
                "returned": item.get("returned", 0),
                "exceededTransferLimit": bool(item.get("exceededTransferLimit")),
            }
            for item in result_sets
            if isinstance(item, dict)
        ],
        "warnings": map_result.get("warnings", []),
    }
