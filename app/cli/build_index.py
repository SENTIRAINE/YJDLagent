from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.rag.indexer import build_index


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the local RAG index from PDF files")
    parser.add_argument("sources", nargs="*", type=Path, help="Optional PDF paths; defaults to RAG_SOURCE_GLOB")
    args = parser.parse_args()
    manifest = build_index(Settings.from_env(), source_paths=args.sources or None)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

