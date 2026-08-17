from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.rag.retriever import HybridRetriever


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local RAG index")
    parser.add_argument("query", help="Chinese knowledge-base query")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--context", action="store_true", help="Print LLM-ready context instead of JSON")
    args = parser.parse_args()

    retriever = HybridRetriever(Settings.from_env())
    results = retriever.search(args.query, top_k=args.top_k)
    if args.context:
        print(retriever.format_context(results))
    else:
        print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

