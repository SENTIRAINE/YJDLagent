from __future__ import annotations

import argparse
from collections import Counter
import json
import sys

from pymongo import MongoClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an isolated restored Agent Mongo database")
    parser.add_argument("--uri", required=True)
    parser.add_argument("--database", default="yjdl_agent_restore")
    args = parser.parse_args()

    client = MongoClient(args.uri, serverSelectionTimeoutMS=5000)
    db = client[args.database]
    client.admin.command("ping")

    issues: list[str] = []
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED"}
    for run in db.agent_runs.find({}, {"run_id": 1, "status": 1, "last_sequence": 1}):
        events = list(db.agent_events.find({"run_id": run["run_id"]}, {"sequence": 1, "event_name": 1}).sort("sequence", 1))
        sequences = [int(event["sequence"]) for event in events]
        expected = list(range(1, int(run.get("last_sequence", 0)) + 1))
        if sequences != expected:
            issues.append(f"{run['run_id']}: event sequence gap")
        completed = Counter(event["event_name"] for event in events)["run.completed"]
        memory = db.conversation_memory.count_documents({"run_id": run["run_id"]})
        if run.get("status") == "SUCCEEDED" and (completed != 1 or memory != 1):
            issues.append(f"{run['run_id']}: succeeded run/memory/completed mismatch")
        if run.get("status") in terminal and completed > 1:
            issues.append(f"{run['run_id']}: duplicate run.completed")

    report = {
        "database": args.database,
        "runs": db.agent_runs.estimated_document_count(),
        "events": db.agent_events.estimated_document_count(),
        "memories": db.conversation_memory.estimated_document_count(),
        "conversationStates": db.conversation_states.estimated_document_count(),
        "checkpoints": db.agent_checkpoints.estimated_document_count(),
        "issues": issues,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    client.close()
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())

