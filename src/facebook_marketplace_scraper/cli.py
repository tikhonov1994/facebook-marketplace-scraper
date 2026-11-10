"""Local login and one-shot JSON collection commands."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .collector import FacebookMarketplaceCollector
from .config import Settings
from .login import main as login


async def collect(output: Path, known_ids: Path | None = None) -> int:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    known = set()
    if known_ids:
        values = json.loads(known_ids.read_text(encoding="utf-8"))
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("Known IDs must be a JSON array of strings")
        known = set(values)
    rows = await FacebookMarketplaceCollector(Settings()).fetch(known_external_ids=known)
    payload = json.dumps([row.model_dump(mode="json") for row in rows], ensure_ascii=False, indent=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")
    logging.getLogger(__name__).info("Saved %d listings to %s", len(rows), output)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Facebook Marketplace property listings")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Open a dedicated browser for manual Facebook login")
    command = commands.add_parser("collect", help="Collect once and write a JSON array")
    command.add_argument("--output", required=True, type=Path, help="New JSON output file")
    command.add_argument("--known-ids", type=Path, help="JSON array of IDs to skip")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "login":
            asyncio.run(login())
        else:
            asyncio.run(collect(args.output, args.known_ids))
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted\n")
    except Exception as error:
        parser.exit(1, f"{error}\n")
