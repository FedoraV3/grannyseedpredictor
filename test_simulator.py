"""
test_simulator.py

Acceptance test: simulate(915074960) must reproduce the real-game placement
captured in dumps/ exactly (31/31 items on the right slot).
"""

import glob
from pathlib import Path

import ground_truth as gt
import simulator


def _newest_config_path() -> str:
    dumps_dir = Path(__file__).parent / "dumps"
    config_files = sorted(dumps_dir.glob("config_*.json"))
    assert config_files, f"No config_*.json files found in {dumps_dir}"
    return str(config_files[-1])


def test_simulate_matches_ground_truth():
    cfg_dump, placement_dump = gt.load_dumps()
    truth = gt.build_ground_truth(cfg_dump, placement_dump)

    cfg = simulator.load_config(_newest_config_path())
    seed = cfg["gameSeed"]
    assert seed == 915074960, f"expected seed 915074960 from config, got {seed}"

    simulated = simulator.simulate(seed, cfg)

    ok, diffs = gt.compare(truth, simulated)

    if not ok:
        print(f"\n{len(diffs)} mismatch(es) out of {len(truth)} items:")
        for line in diffs:
            print(f"  {line}")

    assert ok, f"simulate({seed}) diverged from ground truth: {diffs}"
    assert len(simulated) == 31, f"expected 31 placed items, got {len(simulated)}"
    print(f"\nPASS: {len(truth)}/{len(truth)} items matched ground truth exactly.")


if __name__ == "__main__":
    test_simulate_matches_ground_truth()
