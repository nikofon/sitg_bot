"""Measure management SQL, service time, and JSON bytes on disposable SQLite data.

Run: python scripts/benchmark_management_loading.py --baseline-ref HEAD
Browser-side approximation: cd web && VITE_MANAGEMENT_BENCHMARK=1 npm test -- \
  src/management-loading.test.ts -t 'opens 200 packets'
"""

import argparse
import asyncio
import importlib.util
import json
import subprocess
import sys
import time
import types
from functools import partial
from pathlib import Path

from sitg_bot.application.gateway import ApplicationGateway
from sitg_bot.services.tournaments import TournamentService

ROOT = Path(__file__).resolve().parents[1]


def fixture_module():
    spec = importlib.util.spec_from_file_location(
        "_management_fixture", ROOT / "tests/unit/test_management_loading.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_service(ref):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:src/sitg_bot/services/tournaments.py"], cwd=ROOT, text=True,
    )
    module = types.ModuleType("_baseline_management")
    sys.modules[module.__name__] = module
    exec(compile(source, "baseline_tournaments.py", "exec"), module.__dict__)
    return module.TournamentService


async def main(args):
    fixture = fixture_module()
    baseline = baseline_service(args.baseline_ref)
    print("packets,players,mode,sql_selects,service_seconds,json_bytes")
    for packets in args.packets:
        for players in args.players:
            database = fixture.ManagementDatabase()
            try:
                data = fixture.seed(
                    database, packets=packets, players=players, content=args.content,
                )
                old, current = baseline(database), TournamentService(database)
                results = {}
                for mode, call in (
                    ("baseline_full", partial(old.manager_management,
                        data.tournament_id, data.manager_id)),
                    ("batched_full", partial(current.manager_management,
                        data.tournament_id, data.manager_id)),
                    ("deferred_initial", partial(current.manager_management,
                        data.tournament_id, data.manager_id, include_player_access=False)),
                    ("selected_packet", partial(current.manager_packet_access,
                        data.tournament_id, data.assignment_ids[0], data.manager_id)),
                ):
                    database.queries.clear()
                    start = time.perf_counter()
                    result = await call()
                    elapsed = time.perf_counter() - start
                    value = ApplicationGateway._json_value(result)
                    encoded = json.dumps(value, ensure_ascii=False).encode()
                    results[mode] = value
                    print(f"{packets},{players},{mode},{len(database.queries)},"
                          f"{elapsed:.4f},{len(encoded)}", flush=True)
                assert results["baseline_full"] == results["batched_full"]
            finally:
                database.engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD")
    parser.add_argument("--packets", type=int, nargs="+", default=[50, 100, 200])
    parser.add_argument("--players", type=int, nargs="+", default=[10, 20])
    parser.add_argument("--content", action="store_true",
                        help="Add one theme, question, and coauthor per packet")
    asyncio.run(main(parser.parse_args()))
