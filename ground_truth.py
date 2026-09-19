"""
Load SeedDumper JSON dumps and produce canonical {itemName -> slot_label} mapping.

Used as the acceptance test for a simulator.
"""

import json
import glob
import math
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional


class Slot(NamedTuple):
    """A placeable slot in the world."""
    kind: str  # "FREE" or "PUZZLE"
    label: str  # "Area/Spot (n)" or "PuzzleName"
    position: tuple[float, float, float]  # (x, y, z)
    area_name: Optional[str] = None  # For FREE slots only
    spot_name: Optional[str] = None  # For FREE slots only


def _euclidean_distance(pos1: dict, pos2: tuple) -> float:
    """Compute 3D euclidean distance between position dict {x,y,z} and tuple (x,y,z)."""
    dx = pos1["x"] - pos2[0]
    dy = pos1["y"] - pos2[1]
    dz = pos1["z"] - pos2[2]
    return math.sqrt(dx*dx + dy*dy + dz*dz)


def _pos(pos_dict: dict) -> tuple[float, float, float]:
    """Convert position dict to tuple."""
    return (pos_dict["x"], pos_dict["y"], pos_dict["z"])


def load_dumps(config_path: Optional[str] = None, placement_path: Optional[str] = None) -> tuple:
    """
    Load config and placement JSON dumps.

    If paths not provided, uses newest files by filename sort from dumps directory.

    Returns:
        (cfg dict, placement dict)
    """
    dumps_dir = Path(__file__).parent / "dumps"

    if not config_path:
        config_files = sorted(dumps_dir.glob("config_*.json"))
        if not config_files:
            raise FileNotFoundError(f"No config_*.json files found in {dumps_dir}")
        config_path = str(config_files[-1])

    if not placement_path:
        placement_files = sorted(dumps_dir.glob("placement_*.json"))
        if not placement_files:
            raise FileNotFoundError(f"No placement_*.json files found in {dumps_dir}")
        placement_path = str(placement_files[-1])

    with open(config_path) as f:
        cfg = json.load(f)

    with open(placement_path) as f:
        placement = json.load(f)

    return cfg, placement


def build_slots(cfg: dict) -> list[Slot]:
    """
    Extract all placeable slots from config.

    Returns list of Slot objects, sorted by kind (FREE first, then PUZZLE).
    """
    slots = []

    # Extract FREE slots from spawnAreas
    for area in cfg.get("spawnAreas", []):
        area_name = area.get("areaName", "")
        for spot in area.get("freeSpots", []):
            if spot.get("position"):
                pos = _pos(spot["position"])
                label = f"{area_name}/{spot['name']}"
                slots.append(Slot(
                    kind="FREE",
                    label=label,
                    position=pos,
                    area_name=area_name,
                    spot_name=spot["name"]
                ))

    # Extract PUZZLE slots from puzzleDefs
    for puzzle in cfg.get("puzzleDefs", []):
        sp = puzzle.get("spawnPoint")
        if sp and sp.get("position"):
            pos = _pos(sp["position"])
            label = puzzle.get("puzzleName", "")
            slots.append(Slot(
                kind="PUZZLE",
                label=label,
                position=pos,
                area_name=None,
                spot_name=None
            ))

    return slots


def build_ground_truth(cfg: dict, placement: dict, tol: float = 0.01) -> dict[str, str]:
    """
    Build canonical {itemName -> slot_label} mapping by matching placed items to slots.

    For each placed item, finds the nearest slot by 3D euclidean distance.
    Raises clear error if any item fails to match a slot within tolerance.

    Args:
        cfg: Config dump dict
        placement: Placement dump dict
        tol: Maximum distance tolerance for exact match (default 0.01)

    Returns:
        dict mapping itemName -> slot_label
            where slot_label is "FREE:Area/Spot (n)" or "PUZZLE:PuzzleName"

    Raises:
        ValueError: If any placed item doesn't match a slot within tolerance
    """
    slots = build_slots(cfg)
    ground_truth = {}

    for item in placement.get("items", []):
        item_name = item.get("itemName")
        item_pos = item.get("position")

        if not item_name or not item_pos:
            continue

        # Find nearest slot
        nearest_slot = None
        nearest_dist = float("inf")

        for slot in slots:
            dist = _euclidean_distance(item_pos, slot.position)
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_slot = slot

        if nearest_slot is None:
            raise ValueError(f"Item {item_name} at {item_pos} has no slots to match against")

        if nearest_dist > tol:
            raise ValueError(
                f"Item {item_name} at {_pos(item_pos)} failed to match any slot. "
                f"Nearest slot: {nearest_slot.kind}:{nearest_slot.label} "
                f"at {nearest_slot.position} (distance: {nearest_dist:.6f}, tolerance: {tol})"
            )

        # Build label
        if nearest_slot.kind == "FREE":
            slot_label = f"FREE:{nearest_slot.label}"
        else:
            slot_label = f"PUZZLE:{nearest_slot.label}"

        ground_truth[item_name] = slot_label

    return ground_truth


def report(gt: dict[str, str], cfg: dict, placement: dict) -> str:
    """
    Generate a human-readable report table.

    Args:
        gt: Ground truth mapping from build_ground_truth()
        cfg: Config dump dict
        placement: Placement dump dict

    Returns:
        Formatted report string
    """
    lines = []

    # Count slots by kind
    slots = build_slots(cfg)
    puzzle_count = sum(1 for s in slots if s.kind == "PUZZLE")
    free_count = sum(1 for s in slots if s.kind == "FREE")

    lines.append(f"Ground Truth Report")
    lines.append(f"==================")
    lines.append(f"Total items placed: {len(gt)}")
    lines.append(f"PUZZLE slots: {puzzle_count}")
    lines.append(f"FREE slots: {free_count}")
    lines.append(f"")
    lines.append(f"Item Placements:")
    lines.append(f"{'-' * 60}")

    # Sort by kind (PUZZLE first) then by label
    sorted_items = sorted(gt.items(), key=lambda x: (not x[1].startswith("PUZZLE:"), x[1]))

    for item_name, slot_label in sorted_items:
        lines.append(f"{item_name:20} -> {slot_label}")

    lines.append(f"{'-' * 60}")

    # Count PUZZLE and FREE placements in GT
    puzzle_placements = sum(1 for s in gt.values() if s.startswith("PUZZLE:"))
    free_placements = sum(1 for s in gt.values() if s.startswith("FREE:"))
    lines.append(f"PUZZLE placements: {puzzle_placements}")
    lines.append(f"FREE placements: {free_placements}")

    return "\n".join(lines)


def compare(
    gt: dict[str, str],
    simulated: dict[str, str]
) -> tuple[bool, list[str]]:
    """
    Compare ground truth against simulated placement.

    This is the acceptance-test entry point a simulator will call.

    Args:
        gt: Ground truth mapping
        simulated: Simulated placement mapping

    Returns:
        (all_match: bool, diff_lines: list[str])
        where diff_lines contains human-readable differences like:
            "Hammer: expected FREE:Kitchen/Spot (35), got PUZZLE:Safe"
    """
    diff_lines = []

    # Check all items in ground truth
    all_items = set(gt.keys()) | set(simulated.keys())

    for item_name in sorted(all_items):
        expected = gt.get(item_name)
        actual = simulated.get(item_name)

        if expected != actual:
            if expected is None:
                diff_lines.append(f"{item_name}: unexpected in simulated, got {actual}")
            elif actual is None:
                diff_lines.append(f"{item_name}: expected {expected}, missing from simulated")
            else:
                diff_lines.append(f"{item_name}: expected {expected}, got {actual}")

    return len(diff_lines) == 0, diff_lines


if __name__ == "__main__":
    cfg, placement = load_dumps()

    gt = build_ground_truth(cfg, placement)
    print(report(gt, cfg, placement))
    print()

    # Verify all items matched
    print(f"Total items matched: {len(gt)}/31")

    # Spot-check known-correct mappings
    spot_checks = {
        "Pliers": "FREE:OldHouse/Spot (82)",
        "RustyKey": "PUZZLE:Melon",
        "SparkPlug": "PUZZLE:ScrewHoleCellar",
        "Code": "FREE:Cellar/Spot (72)",
        "WheelCrank": "PUZZLE:BirdCage",
    }

    print()
    print("Spot checks:")
    all_correct = True
    for item_name, expected in spot_checks.items():
        actual = gt.get(item_name)
        status = "OK" if actual == expected else "FAIL"
        print(f"{status} {item_name}: {actual}")
        if actual != expected:
            all_correct = False

    if not all_correct:
        raise AssertionError("Spot checks failed!")

    print()
    print("All items matched exactly. Spot checks passed.")
