"""Run unittest discovery in isolated processes, one module per process."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


def test_groups(suite):
    """Group discovered test IDs without dropping or duplicating cases."""
    groups = {}

    def collect(item):
        if isinstance(item, unittest.TestSuite):
            for child in item:
                collect(child)
        else:
            groups.setdefault(item.__class__.__module__, []).append(item.id())

    collect(suite)
    return groups


def run_module(item):
    module, ids = item
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *ids], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=120,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    return module, len(ids), time.perf_counter() - started, result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    # Importable package names let each child use the exact discovered test IDs.
    sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    if unittest.defaultTestLoader.errors:
        for error in unittest.defaultTestLoader.errors:
            print(error, file=sys.stderr)
        return 1
    groups = test_groups(suite)
    if not groups:
        print("No tests discovered", file=sys.stderr)
        return 1
    started = time.perf_counter()
    failed = False
    # Environment patches and application globals stay inside their own process.
    # Fixtures use unique temporary directories and ephemeral loopback ports.
    with ThreadPoolExecutor(max_workers=min(args.jobs, len(groups))) as pool:
        for module, count, elapsed, result in pool.map(run_module, groups.items()):
            print(f"{module}: {count} tests, {elapsed:.3f}s", flush=True)
            print(result.stdout, end="", flush=True)
            failed |= result.returncode != 0
    print(f"Total: {suite.countTestCases()} tests, {time.perf_counter() - started:.3f}s", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
