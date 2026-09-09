import argparse
import asyncio
import json
import os
from datetime import date, datetime
from pathlib import Path
from uuid import UUID

from sitg_bot.services.packets import PacketAdminService
from sitg_bot.storage.database import Database


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__}")


async def main(args: argparse.Namespace) -> None:
    database = Database(args.database_url)
    service = PacketAdminService(database)
    try:
        if args.command == "import":
            print(await service.import_json(args.path, tournament_id=args.tournament_id))
        elif args.command == "preview":
            print(
                json.dumps(
                    await service.preview(args.draft_id),
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                )
            )
        elif args.command == "publish":
            packet = await service.publish(args.draft_id)
            print(packet.version_id)
        elif args.command == "reject":
            await service.reject(args.draft_id)
    finally:
        await database.close()


def run() -> None:
    parser = argparse.ArgumentParser(description="Import and publish database packets")
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://sitg:sitg@localhost:5432/sitg"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    import_parser = commands.add_parser("import", help="validate JSON and create a draft")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--tournament-id", type=UUID, required=True)
    preview_parser = commands.add_parser("preview", help="render the exact draft")
    preview_parser.add_argument("draft_id", type=UUID)
    publish_parser = commands.add_parser("publish", help="publish a confirmed draft")
    publish_parser.add_argument("draft_id", type=UUID)
    reject_parser = commands.add_parser("reject", help="reject a draft")
    reject_parser.add_argument("draft_id", type=UUID)
    asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    run()
