from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.agent.mongo_store import MongoAgentStore
from app.agent.store import AgentStore


def migrate(sqlite_path: Path, mongo_uri: str, mongo_database: str, *, require_transactions: bool) -> tuple[int, int, int]:
    source = AgentStore(sqlite_path)
    target = MongoAgentStore(mongo_uri, mongo_database, require_transactions=require_transactions)
    runs = events = memories = 0
    with source._connect() as connection:  # migration is an offline administrative command
        for row in connection.execute("SELECT * FROM agent_runs ORDER BY created_at").fetchall():
            document = {
                "run_id": row["run_id"], "conversation_id": row["conversation_id"], "message_id": row["message_id"],
                "tenant_id": row["tenant_id"], "user_id": row["user_id"], "trace_id": row["trace_id"],
                "status": row["status"], "request_hash": row["request_hash"], "request": json.loads(row["request_json"]),
                "last_sequence": row["last_sequence"], "route": row["route"], "answer": row["answer"],
                "citations": json.loads(row["citations_json"]), "warnings": json.loads(row["warnings_json"]),
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "lease_owner": row["lease_owner"], "lease_until": row["lease_until"], "lease_generation": row["lease_generation"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
            target.runs.replace_one({"run_id": document["run_id"]}, document, upsert=True); runs += 1
        for row in connection.execute("SELECT * FROM agent_events ORDER BY run_id, sequence").fetchall():
            target.events.replace_one({"run_id": row["run_id"], "sequence": row["sequence"]}, {"run_id": row["run_id"], "sequence": row["sequence"], "event_name": row["event_name"], "event_id": row["event_id"], "data": json.loads(row["data_json"]), "created_at": row["created_at"]}, upsert=True); events += 1
        for row in connection.execute("SELECT * FROM conversation_memory ORDER BY id").fetchall():
            document = {"tenant_id": row["tenant_id"], "user_id": row["user_id"], "conversation_id": row["conversation_id"], "user_query": row["user_query"], "assistant_answer": row["assistant_answer"], "route": row["route"], "map_summary": json.loads(row["map_summary_json"]) if row["map_summary_json"] else None, "created_at": row["created_at"]}
            filter_key = {"tenant_id": row["tenant_id"], "user_id": row["user_id"], "conversation_id": row["conversation_id"], "created_at": row["created_at"]}
            if row["run_id"]:
                document["run_id"] = row["run_id"]
                filter_key = {"run_id": row["run_id"]}
            target.memory.replace_one(filter_key, document, upsert=True); memories += 1
    target.close()
    return runs, events, memories


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate Agent Run/Event/Conversation data from SQLite to MongoDB")
    parser.add_argument("--sqlite", required=True, type=Path)
    parser.add_argument("--mongo-uri", required=True)
    parser.add_argument("--mongo-database", default="yjdl_agent")
    parser.add_argument("--allow-no-transactions", action="store_true", help="only for local test Mongo implementations")
    args = parser.parse_args()
    counts = migrate(args.sqlite, args.mongo_uri, args.mongo_database, require_transactions=not args.allow_no_transactions)
    print(f"migrated runs={counts[0]} events={counts[1]} memories={counts[2]}")


if __name__ == "__main__":
    main()
