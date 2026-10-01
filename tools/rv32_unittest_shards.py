"""Run one unittest file in several processes at once (issue #26).

    python3 tools/rv32_unittest_shards.py --shards 4 test_rv32_rtl.py

discovers the tests as `python -m unittest discover -s tests -p FILE` would and deals them out
round-robin to N `python -m unittest -v` processes. Each process runs from the repository root, as
the make recipes do, so relative build/ paths mean the same thing. A class's setUpClass then runs
once in every shard that holds one of its tests. That is the price of the split, so it pays only
for suites whose tests are slow next to their setup (the Icarus sweep of test_rv32_rtl.py).

Each shard's log is printed whole when the shard finishes. The run fails if any shard fails, or
if the shards together ran a different number of tests than discovery found, so a test cannot
drop out unnoticed.
"""
import argparse
import os
import re
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def test_ids(pattern):
    """Every test id in the files matching `pattern`, in discovery order."""
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item.id()
    sys.path[:0] = [str(TESTS), str(ROOT)]
    return list(flatten(unittest.defaultTestLoader.discover(str(TESTS), pattern=pattern, top_level_dir=str(TESTS))))


def run_shard(index, ids):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(TESTS), str(ROOT), env.get("PYTHONPATH")]))
    start = time.time()
    result = subprocess.run([sys.executable, "-m", "unittest", "-v", *ids], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ran = re.findall(r"^Ran (\d+) tests? in", result.stdout, re.MULTILINE)
    return index, result.returncode, int(ran[-1]) if ran else 0, time.time() - start, result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pattern", help="test file name or glob under tests/, as for unittest discover -p")
    parser.add_argument("--shards", type=int, default=int(os.environ.get("RV32_TEST_SHARDS", "4")),
                        help="processes to split the tests over (default $RV32_TEST_SHARDS or 4)")
    args = parser.parse_args(argv)
    if args.shards < 1:
        parser.error("--shards must be at least 1")
    ids = test_ids(args.pattern)
    if not ids:
        sys.exit(f"no tests match {args.pattern}")
    shards = [ids[i::args.shards] for i in range(min(args.shards, len(ids)))]
    print(f"{len(ids)} tests from {args.pattern} in {len(shards)} shard(s)", flush=True)
    failed, total = [], 0
    with ThreadPoolExecutor(len(shards)) as pool:
        futures = [pool.submit(run_shard, index, shard) for index, shard in enumerate(shards)]
        for future in as_completed(futures):
            index, status, ran, seconds, output = future.result()
            print(f"--- shard {index + 1}/{len(shards)}: {ran} tests, {seconds:.1f} s, exit {status} ---")
            print(output, end="" if output.endswith("\n") else "\n", flush=True)
            total += ran
            if status != 0:
                failed.append(index + 1)
    if failed:
        sys.exit(f"shard(s) {', '.join(map(str, sorted(failed)))} failed")
    if total != len(ids):
        sys.exit(f"the shards ran {total} tests but discovery found {len(ids)}")
    print(f"all {total} tests passed in {len(shards)} shard(s)")


if __name__ == "__main__":
    main()
