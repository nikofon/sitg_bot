"""Compare lobby packet work with a Git baseline on disposable PostgreSQL data.

Set TEST_DATABASE_URL to a disposable database; this creates fixtures and runs migrations.
Run: .venv/bin/python scripts/benchmark_lobby_packets.py --baseline-ref HEAD
Measures service-process CPU and SQL, excluding HTTP/authentication and browser rendering.
"""

import argparse
import asyncio
import importlib.util
import subprocess
import sys
import time
import types
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from sqlalchemy import event, select, update

from sitg_bot.services import ruleset_content
from sitg_bot.services.matchmaking import InvitationMatchmakingService
from sitg_bot.services.packets import PacketAdminService
from sitg_bot.storage.database import Database
from sitg_bot.storage.models import TournamentPacketAssignmentRecord, TournamentPolicyVersionRecord

ROOT = Path(__file__).resolve().parents[1]


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "_lobby_benchmark_fixture", ROOT / "tests/integration/test_lobby_architecture.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def baseline_service(ref):
    source = subprocess.check_output([
        "git", "show", f"{ref}:src/sitg_bot/services/matchmaking.py",
    ], cwd=ROOT, text=True)
    module = types.ModuleType("_baseline_lobby_matchmaking")
    sys.modules[module.__name__] = module
    exec(compile(source, "baseline_matchmaking.py", "exec"), module.__dict__)
    return module.InvitationMatchmakingService


async def catalogue(service, lobby_id, owner):
    cursor, found = None, set()
    while True:
        page = await service.packet_page(lobby_id, telegram_user_id=owner, limit=25, cursor=cursor)
        found.update(item.packet_id for item in page["items"])
        if page["next_cursor"] is None:
            return found
        cursor = UUID(page["next_cursor"])


async def measure(database, operation):
    counts = {"sql": 0, "assignment_rows": 0, "exposure_reads": 0}

    def capture(_connection, cursor, statement, _parameters, _context, _many):
        counts["sql"] += 1
        if statement.startswith("SELECT logical_packets.id,"):
            counts["assignment_rows"] += cursor.rowcount
        if statement.startswith("SELECT player_exposure_claims.player_id,"):
            counts["exposure_reads"] += 1

    event.listen(database.engine.sync_engine, "after_cursor_execute", capture)
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        await operation()
    finally:
        elapsed, cpu_time = time.perf_counter() - wall, time.process_time() - cpu
        event.remove(database.engine.sync_engine, "after_cursor_execute", capture)
    return (f"{elapsed:.4f},{cpu_time:.4f},{counts['sql']},"
            f"{counts['assignment_rows']},{counts['exposure_reads']}")


async def main(url, fixture_module, baseline):
    database = Database(url)
    try:
        fixture = await fixture_module.tournament_fixture(
            database, player_count=2, hybrid_matchmaking_enabled=False,
        )
        packets = PacketAdminService(database)
        packet_ids = []
        for index in range(200):
            source = fixture_module.packet()
            content = replace(source, name=f"Benchmark {index:03}", themes=tuple(
                replace(source.themes[0], name=f"Theme {position}", questions=tuple(
                    replace(question, text=f"Packet {index} theme {position}: {question.text}")
                    for question in source.themes[0].questions
                )) for position in range(8)
            ))
            draft = await packets.create_draft(
                content, source_filename=f"benchmark-{index}.json", uploader_id=fixture.manager.id,
                tournament_id=fixture.tournament_id,
            )
            stored = await packets.publish(draft, administrator_id=fixture.manager.id)
            packet_ids.append(stored.logical_id)
            if (index + 1) % 50 == 0:
                print(f"Prepared {index + 1}/200 packets", file=sys.stderr, flush=True)
        async with database.transaction() as session:
            policy = await session.scalar(select(TournamentPolicyVersionRecord).where(
                TournamentPolicyVersionRecord.tournament_id == fixture.tournament_id,
            ))
            policy.policies = {**policy.policies, "packets_per_lobby": "any"}
            await session.execute(update(TournamentPacketAssignmentRecord).where(
                TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
            ).values(discoverable_by_members=True, playable_by_members=True))
        current = InvitationMatchmakingService(database)
        owner = fixture.inputs[0].telegram_user_id
        lobby = await current.create_lobby(fixture.inputs[0], tournament_id=fixture.tournament_id)
        await current.join(lobby.invitation_code, fixture.inputs[1])
        await current.select_packet(lobby.id, owner, packet_ids[1])
        print("packets,operation,cache,mode,wall_seconds,cpu_seconds,sql,assignment_rows,exposure_reads",
              flush=True)
        for size in (50, 100, 200):
            async with database.transaction() as session:
                await session.execute(update(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                ).values(status="retired"))
                await session.execute(update(TournamentPacketAssignmentRecord).where(
                    TournamentPacketAssignmentRecord.tournament_id == fixture.tournament_id,
                    TournamentPacketAssignmentRecord.packet_id.in_(packet_ids[:size]),
                ).values(status="active"))
            for mode, service in (("before", baseline(database)), ("after", current)):
                for cache in ("cold", "warm"):
                    async def toggle_cycle(service=service, mode=mode, size=size):
                        for method in (service.select_packet, service.remove_packet):
                            options = {"include_snapshot": False} if mode == "after" else {}
                            await method(lobby.id, owner, packet_ids[0], **options)
                            snapshot = await service.get(lobby.id)
                            assert len(snapshot.selected_packets) in (1, 2)
                            if mode == "before":
                                assert len(await catalogue(service, lobby.id, owner)) == size

                    ruleset_content._CACHES.clear()
                    if cache == "warm":
                        await catalogue(service, lobby.id, owner)
                    result = await measure(database, toggle_cycle)
                    print(f"{size},select_remove_cycle,{cache},{mode},{result}", flush=True)
                    ruleset_content._CACHES.clear()
                    if cache == "warm":
                        await catalogue(service, lobby.id, owner)
                    result = await measure(database, lambda service=service: catalogue(
                        service, lobby.id, owner,
                    ))
                    print(f"{size},catalogue,{cache},{mode},{result}", flush=True)
    finally:
        await database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD")
    args = parser.parse_args()
    fixture_module = load_fixture()
    url = fixture_module.database_url.__wrapped__()
    asyncio.run(main(url, fixture_module, baseline_service(args.baseline_ref)))
