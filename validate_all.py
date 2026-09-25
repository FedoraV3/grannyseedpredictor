"""
Validation harness for simulator.py against all ground-truth dump pairs.

This is a regression suite: validates the simulator against every captured
config/placement pair, not just the newest.
"""

from pathlib import Path
from typing import Optional, NamedTuple

from dump_utils import capture_stamp, pair_dumps


class ValidatePair(NamedTuple):
    """Result of validating a single pair."""
    passed: bool
    matched_count: int
    total_count: int
    diff_lines: list[str]
    error: Optional[str] = None


def discover_pairs(dumps_dir: str = "dumps") -> list[tuple[Path, Path, int]]:
    """
    Discover all (config, placement, seed) tuples from dumps directory.

    Pairing is dump_utils.pair_dumps: files are ordered by their own
    capturedAtUtc, each placement goes with the config captured last before
    it, and the pair must agree on callIndex and seed. Unpaired files are
    skipped with a warning giving the reason.

    Args:
        dumps_dir: Path to dumps directory

    Returns:
        list of (config_path, placement_path, seed) tuples
        where seed comes from the placement's ["seed"] field.

    Raises:
        FileNotFoundError: If dumps_dir does not exist
    """
    dumps_path = Path(dumps_dir)
    if not dumps_path.exists():
        raise FileNotFoundError(f"Dumps directory not found: {dumps_path.absolute()}")

    if not any(dumps_path.glob("config_*.json")) or not any(dumps_path.glob("placement_*.json")):
        raise FileNotFoundError(
            f"No config_*.json or placement_*.json files found in {dumps_path.absolute()}"
        )

    pairing = pair_dumps(dumps_path)
    for path, reason in sorted(pairing.unpaired.items()):
        print(f"WARNING: Skipping {path.name}: {reason}")

    return [(p.config, p.placement, p.seed) for p in pairing.pairs]


def validate_pair(config_path: Path, placement_path: Path, seed: int) -> ValidatePair:
    """
    Validate a single config/placement pair.

    Loads the config, simulates using the provided seed, builds ground truth
    from the placement, and compares.

    Args:
        config_path: Path to config dump JSON
        placement_path: Path to placement dump JSON
        seed: The seed value from placement["seed"]

    Returns:
        ValidatePair with passed/failed status and diff details.
        If the config lacks position data (insufficient data to build ground
        truth), error is set and passed=False but this is distinguished from
        a true validation failure.
    """
    # Import here to avoid circular dependency
    from simulator import load_config, simulate
    from ground_truth import load_dumps, build_ground_truth, compare

    try:
        # Load raw config and placement from dumps (not the processed version)
        cfg_raw, placement = load_dumps(str(config_path), str(placement_path))

        # Try to build ground truth
        try:
            gt = build_ground_truth(cfg_raw, placement)
        except ValueError as e:
            # Insufficient data (e.g., missing position fields)
            return ValidatePair(
                passed=False,
                matched_count=0,
                total_count=0,
                diff_lines=[],
                error=str(e)
            )

        # Load processed config for simulation
        cfg = load_config(str(config_path))

        # Simulate
        simulated = simulate(seed, cfg)

        # Compare
        all_match, diff_lines = compare(gt, simulated)
        matched_count = len(gt) - len(diff_lines)
        total_count = len(gt)

        return ValidatePair(
            passed=all_match,
            matched_count=matched_count,
            total_count=total_count,
            diff_lines=diff_lines,
            error=None
        )

    except Exception as e:
        return ValidatePair(
            passed=False,
            matched_count=0,
            total_count=0,
            diff_lines=[],
            error=f"Unexpected error: {type(e).__name__}: {e}"
        )


def main() -> int:
    """
    Run validation against all discovered pairs.

    Prints a summary table and individual failure details.
    Exits with 0 only if all pairs pass; non-zero otherwise.

    Returns:
        0 if all samples passed, 1 otherwise
    """
    import os

    # Find dumps directory relative to this script
    script_dir = Path(__file__).parent
    dumps_dir = script_dir / "dumps"

    try:
        pairs = discover_pairs(str(dumps_dir))
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    if not pairs:
        print("WARNING: No valid config/placement pairs found in dumps directory")
        return 1

    print("=" * 80)
    print("VALIDATION HARNESS - SIMULATOR REGRESSION TEST")
    print("=" * 80)
    print()

    results: list[tuple[int, str, ValidatePair]] = []

    for config_path, placement_path, seed in pairs:
        result = validate_pair(config_path, placement_path, seed)
        results.append((seed, config_path.name, result))

    # Print summary table
    print(f"{'SEED':<12} | {'CONFIG TIMESTAMP':<25} | {'MATCHED/TOTAL':<13} | {'STATUS':<7}")
    print("-" * 80)

    passed_count = 0
    skipped_count = 0
    failed_count = 0

    for seed, config_name, result in results:
        # Extract timestamp from config name
        timestamp = capture_stamp(config_name) or "???"

        if result.error:
            # Check if it's an insufficient-data error (SKIPPED) or a real error
            if "failed to match any slot" in result.error.lower() or "no slots to match" in result.error.lower():
                status = "SKIPPED"
                skipped_count += 1
            else:
                status = "ERROR"
                failed_count += 1
        elif result.passed:
            status = "PASS"
            passed_count += 1
        else:
            status = "FAIL"
            failed_count += 1

        ratio = f"{result.matched_count}/{result.total_count}"
        print(f"{seed:<12} | {timestamp:<25} | {ratio:<13} | {status:<7}")

    print("-" * 80)
    print()

    # Print failure details
    failure_count = sum(1 for _, _, r in results if not r.passed)
    if failure_count > 0:
        print("FAILURE DETAILS:")
        print("=" * 80)
        for seed, config_name, result in results:
            if not result.passed:
                timestamp = capture_stamp(config_name) or "???"

                if result.error:
                    print()
                    print(f"SEED {seed} ({timestamp}) - ERROR:")
                    print(f"  {result.error}")
                else:
                    print()
                    print(f"SEED {seed} ({timestamp}) - FAILED ({result.matched_count}/{result.total_count} matched):")
                    # Print first 10 diff lines
                    for i, line in enumerate(result.diff_lines[:10]):
                        print(f"  {line}")
                    if len(result.diff_lines) > 10:
                        print(f"  ... and {len(result.diff_lines) - 10} more differences")

    # Overall verdict
    print()
    print("=" * 80)
    total = len(results)
    if passed_count == 0:
        print(f"NO SAMPLES VALIDATED ({skipped_count} SKIPPED, {failed_count} FAILED, 0 PASSED)")
    elif skipped_count > 0:
        print(f"{passed_count} OF {total} SAMPLE(S) PASSED ({skipped_count} SKIPPED, {failed_count} FAILED)")
    elif failed_count == 0:
        print(f"ALL {total} SAMPLE(S) PASSED")
    else:
        print(f"{failed_count} OF {total} SAMPLE(S) FAILED")
    print("=" * 80)

    # Exit code: 0 only if nothing failed AND at least one sample was
    # actually validated -- an all-SKIPPED run has not proven anything and
    # must not report success.
    return 0 if failed_count == 0 and passed_count > 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
