"""Trusted database-maintenance CLI; preview unless --apply is supplied."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from datetime import timedelta

from sitg_bot.services.database_maintenance import DatabaseMaintenanceService, RetentionPolicy
from sitg_bot.storage.database import Database


def add_retention_arguments(parser: argparse.ArgumentParser) -> None:
    for flag, environment, default, unit in (
        ("success-retention-days", "SITG_DB_SUCCESS_RETENTION_DAYS", 7, "days"),
        ("failure-retention-days", "SITG_DB_FAILURE_RETENTION_DAYS", 30, "days"),
        ("expiry-grace-hours", "SITG_DB_EXPIRY_GRACE_HOURS", 24, "hours"),
    ):
        parser.add_argument(
            f"--{flag}", type=float, default=os.environ.get(environment, default),
            help=f"retention in {unit} (environment: {environment}; default: {default})",
        )


def retention_policy(args: argparse.Namespace) -> RetentionPolicy:
    return RetentionPolicy(
        success=timedelta(days=args.success_retention_days),
        failure=timedelta(days=args.failure_retention_days),
        expiry_grace=timedelta(hours=args.expiry_grace_hours),
    )


async def main(args: argparse.Namespace) -> None:
    policy = retention_policy(args)
    database = Database(args.database_url)
    try:
        service = DatabaseMaintenanceService(
            database, policy=policy, batch_size=args.batch_size,
            max_batches=args.max_batches, max_runtime=args.max_runtime,
        )
        report = await service.run_once(dry_run=not args.apply)
        print(json.dumps({
            **asdict(report),
            "retention_seconds": {
                name: getattr(policy, name).total_seconds()
                for name in ("success", "failure", "expiry_grace")
            },
        }, indent=2, default=str))
    finally:
        await database.close()


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", default=os.environ.get(
            "DATABASE_URL", "postgresql+asyncpg://sitg:sitg@localhost:5432/sitg",
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="delete and compact eligible records")
    mode.add_argument("--dry-run", action="store_true", help="preview eligible records (default)")
    add_retention_arguments(parser)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=100)
    parser.add_argument("--max-runtime", type=float, default=10)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (ValueError, OverflowError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    run()
