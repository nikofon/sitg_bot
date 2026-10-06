#!/usr/bin/env python3
"""Read-only Linux load recorder. Requires Python 3.11+, standard library only.

Run on the VPS host: python3 scripts/monitor_load.py --duration 1800 --docker
Output is flushed JSON Lines; Ctrl+C or SIGTERM stops cleanly. No tokens,
environment variables, application payloads, or process arguments are collected.
"""

import argparse
import json
import math
import os
import platform
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def timestamp():
    return datetime.now(UTC).isoformat()


def read_proc(name, errors):
    try:
        return (Path('/proc') / name).read_text()
    except OSError as error:
        errors[name] = str(error)
        return ''


def snapshot():
    errors = {}
    stat = {}
    for line in read_proc('stat', errors).splitlines():
        key, *values = line.split()
        if key.startswith('cpu') or key in {
            'ctxt', 'processes', 'procs_running', 'procs_blocked',
        }:
            stat[key] = [int(value) for value in values]
    memory = {}
    for line in read_proc('meminfo', errors).splitlines():
        key, value, *_ = line.split()
        memory[key.rstrip(':')] = int(value)  # kB, except HugePages counts
    vm = {}
    for line in read_proc('vmstat', errors).splitlines():
        key, value = line.split()
        if key in {'pswpin', 'pswpout', 'pgmajfault', 'oom_kill', 'pgpgin', 'pgpgout'}:
            vm[key] = int(value)
    network = {}
    for line in read_proc('net/dev', errors).splitlines()[2:]:
        name, values = line.split(':', 1)
        network[name.strip()] = [int(value) for value in values.split()]
    disks = {}
    for line in read_proc('diskstats', errors).splitlines():
        _, _, name, *values = line.split()
        if not name.startswith(('loop', 'ram')):
            disks[name] = [int(value) for value in values]
    pressure = {}
    for resource in ('cpu', 'memory', 'io'):
        pressure[resource] = read_proc('pressure/' + resource, errors).strip()
    return {
        'type': 'host', 'timestamp': timestamp(), 'monotonic_s': time.monotonic(),
        'load_average': os.getloadavg(), 'stat': stat, 'meminfo': memory,
        'vmstat': vm, 'net_dev': network, 'diskstats': disks,
        'pressure': pressure, 'errors': errors,
    }


def cpu_percent(current, previous):
    result = {}
    labels = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')
    for name, values in current['stat'].items():
        if not name.startswith('cpu') or name not in previous['stat']:
            continue
        # Guest counters already overlap user/nice; exclude them from the total.
        delta = [
            a - b for a, b in zip(values[:8], previous['stat'][name][:8], strict=False)
        ]
        total = sum(delta)
        if total > 0 and all(value >= 0 for value in delta):
            result[name] = {
                label: round(value * 100 / total, 3)
                for label, value in zip(labels, delta, strict=False)
            }
    return result


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='new JSONL file; never overwrites')
    parser.add_argument('--interval', type=positive_number, default=1, help='host seconds')
    parser.add_argument('--duration', type=positive_number, help='seconds; default until Ctrl+C')
    parser.add_argument('--docker', action='store_true', help='also sample running containers')
    parser.add_argument('--docker-interval', type=positive_number, default=10)
    args = parser.parse_args()
    if platform.system() != 'Linux':
        parser.error('run this script on the Linux VPS host')
    output = args.output or Path(
        'server-load-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ') + '.jsonl'
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    lock = threading.Lock()
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as error:
        parser.error(str(error))
    with os.fdopen(descriptor, 'w') as stream:
        def write(record):
            with lock:
                stream.write(json.dumps(record) + '\n')
                stream.flush()

        def docker_monitor():
            while not stop.is_set():
                started = time.monotonic()
                record = {'type': 'docker', 'timestamp': timestamp()}
                try:
                    completed = subprocess.run(
                        ['docker', 'stats', '--no-stream', '--format', '{{json .}}'],
                        capture_output=True, text=True, timeout=5, check=True,
                    )
                    record['containers'] = [
                        json.loads(line) for line in completed.stdout.splitlines() if line
                    ]
                except (OSError, subprocess.SubprocessError, ValueError) as error:
                    record['error'] = str(error)
                record['collection_s'] = time.monotonic() - started
                write(record)
                stop.wait(max(0, args.docker_interval - record['collection_s']))

        write({
            'type': 'metadata', 'schema_version': 1, 'timestamp': timestamp(),
            'hostname': platform.node(), 'kernel': platform.release(),
            'cpu_count': os.cpu_count(), 'clock_ticks_per_s': os.sysconf('SC_CLK_TCK'),
            'page_size_bytes': os.sysconf('SC_PAGE_SIZE'),
            'interval_s': args.interval, 'duration_s': args.duration,
            'docker_interval_s': args.docker_interval if args.docker else None,
            'counter_format': 'Linux /proc native units and field order; cumulative unless gauge',
        })
        print(f'Recording to {output.resolve()} (Ctrl+C to stop)', flush=True)
        worker = threading.Thread(target=docker_monitor) if args.docker else None
        if worker:
            worker.start()
        start = time.monotonic()
        deadline = start + args.duration if args.duration else math.inf
        previous = None
        next_sample = start
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                collected = time.monotonic()
                current = snapshot()
                current['sample_lateness_s'] = max(0, collected - next_sample)
                current['collection_s'] = time.monotonic() - collected
                if previous:
                    current['elapsed_s'] = current['monotonic_s'] - previous['monotonic_s']
                    current['cpu_percent'] = cpu_percent(current, previous)
                write(current)
                previous = current
                next_sample += args.interval
                if next_sample < time.monotonic():
                    next_sample = time.monotonic() + args.interval
                stop.wait(max(0, min(next_sample, deadline) - time.monotonic()))
        finally:
            stop.set()
            if worker:
                worker.join()
            write({'type': 'end', 'timestamp': timestamp(), 'elapsed_s': time.monotonic() - start})


if __name__ == '__main__':
    main()
