from __future__ import annotations

import asyncio
from dataclasses import replace
import argparse
from pathlib import Path

from app.agent.runtime import AgentRuntime
from app.config import Settings


async def serve() -> None:
    settings = replace(Settings.from_env(), agent_worker_enabled=True)
    runtime = AgentRuntime(settings)
    try:
        await runtime.initialize("agent-worker-startup")
        runtime.start_workers()
        await asyncio.Event().wait()
    finally:
        await runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the leased Agent Worker")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="load environment variables from a dotenv file before starting",
    )
    args = parser.parse_args()
    if args.env_file is not None:
        try:
            from dotenv import load_dotenv
        except ImportError as exc:  # pragma: no cover - uvicorn[standard] supplies this in production
            raise SystemExit("python-dotenv is required when --env-file is used") from exc
        if not args.env_file.is_file():
            raise SystemExit(f"Environment file not found: {args.env_file}")
        load_dotenv(args.env_file, override=False)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
