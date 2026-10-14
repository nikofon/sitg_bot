"""Compare full-list and paced content loading on disposable SQLite data.

Run with the development dependencies: python scripts/benchmark_packet_lists.py.
The baseline defaults to HEAD; use --baseline-ref to compare after committing.
"""

import argparse
import asyncio
import importlib.util
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

from sitg_bot.services.packet_catalogue import background_packet_batch
from sitg_bot.services.ruleset_content import PacketSelection, SIContentAdapter

ROOT = Path(__file__).resolve().parents[1]


class CpuSampler:
    """Peak process CPU over 100 ms windows, expressed as percent of one core."""

    def __init__(self):
        self.stop = threading.Event()
        self.peak = 0.0
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        wall, cpu = time.perf_counter(), time.process_time()
        while True:
            stopped = self.stop.wait(0.1)
            next_wall, next_cpu = time.perf_counter(), time.process_time()
            self.peak = max(self.peak, 100 * (next_cpu - cpu) / max(0.1, next_wall - wall))
            wall, cpu = next_wall, next_cpu
            if stopped:
                return


def baseline_adapter(ref):
    source = subprocess.check_output([
        "git", "show", f"{ref}:src/sitg_bot/services/ruleset_content.py",
    ], cwd=ROOT, text=True)
    module = types.ModuleType("_baseline_ruleset_content")
    sys.modules[module.__name__] = module
    exec(compile(source, "baseline_ruleset_content.py", "exec"), module.__dict__)
    return module.SIContentAdapter()


async def measure(adapter, session, selections, groups, *, paced):
    async def load(players):
        if not paced:
            return await adapter.available_play_units(session, selections, players)
        units = []
        for offset in range(0, len(selections), 25):
            async with background_packet_batch():
                units.extend(await adapter.available_play_units(
                    session, selections[offset:offset + 25], players,
                ))
        return units

    sampler = CpuSampler()
    wall, cpu = time.perf_counter(), time.process_time()
    sampler.thread.start()
    results = await asyncio.gather(*(load(players) for players in groups))
    elapsed, cpu_time = time.perf_counter() - wall, time.process_time() - cpu
    sampler.stop.set()
    sampler.thread.join()
    assert all(len(units) == len(selections) * 8 for units in results)
    return elapsed, cpu_time, sampler.peak


async def main(ref):
    spec = importlib.util.spec_from_file_location(
        "_content_benchmark_fixture", ROOT / "tests/unit/test_ruleset_content.py",
    )
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    baseline = baseline_adapter(ref)
    print("packets,lobbies,mode,wall_seconds,cpu_seconds,peak_cpu_percent_100ms")
    for packet_count in (50, 100, 200):
        for lobby_count in (1, 2):
            generator = fixture.content_session.__wrapped__()
            session = next(generator)
            try:
                packets = [fixture.add_packet(session, theme_count=8)
                           for _ in range(packet_count)]
                selections = [PacketSelection(packet.version_id, i + 1)
                              for i, packet in enumerate(packets)]
                groups = [[uuid4(), uuid4()] for _ in range(lobby_count)]
                for mode, adapter, paced in (
                    ("full", baseline, False),
                    ("paced_cold", SIContentAdapter(), True),
                    ("paced_warm", SIContentAdapter(), True),
                ):
                    # Each scenario starts after the preceding batch's cooldown.
                    await asyncio.sleep(0.2)
                    wall, cpu, peak = await measure(adapter, session, selections, groups,
                                                   paced=paced)
                    print(f"{packet_count},{lobby_count},{mode},{wall:.3f},{cpu:.3f},{peak:.1f}")
            finally:
                generator.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="HEAD")
    asyncio.run(main(parser.parse_args().baseline_ref))
