from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json

from pymongo import MongoClient


def cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or apply Agent data retention")
    parser.add_argument("--uri", required=True)
    parser.add_argument("--database", default="yjdl_agent")
    parser.add_argument("--event-days", type=int, default=7)
    parser.add_argument("--memory-days", type=int, default=180)
    parser.add_argument("--checkpoint-days", type=int, default=30)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    client = MongoClient(args.uri, serverSelectionTimeoutMS=5000)
    db = client[args.database]
    filters = {
        "agent_events": {"created_at": {"$lt": cutoff(args.event_days)}},
        "conversation_memory": {"created_at": {"$lt": cutoff(args.memory_days)}},
        "agent_checkpoints": {"created_at": {"$lt": cutoff(args.checkpoint_days)}},
    }
    report: dict[str, object] = {"database": args.database, "execute": args.execute, "collections": {}}
    for name, query in filters.items():
        collection = db[name]
        matched = collection.count_documents(query)
        deleted = collection.delete_many(query).deleted_count if args.execute else 0
        report["collections"][name] = {"matched": matched, "deleted": deleted}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    client.close()


if __name__ == "__main__":
    main()

