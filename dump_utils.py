"""
Shared helpers for locating and pairing captured dump files.

Dump filenames are `{name}_{seed}_{timestamp}.json`, where `seed` is a
variable-width number that sorts BEFORE the timestamp. A plain
lexicographic sort on the filename is therefore not chronological order:
e.g. "config_10000_..." sorts before "config_9999_..." because '1' < '9',
even when the 10000 dump was captured
later. Sort by modification time instead.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional


def newest_dump_file(dumps_dir: Path, pattern: str) -> Path:
    """Return the most recently modified file in `dumps_dir` matching `pattern`.

    Raises FileNotFoundError if no file matches.
    """
    dumps_dir = Path(dumps_dir)
    files = list(dumps_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No files matching {pattern!r} in {dumps_dir}")
    return max(files, key=lambda p: p.stat().st_mtime)


_CAPTURE_STAMP_RE = re.compile(r"_(20\d{6}_\d{6}_\d{3})\.json$")


def capture_stamp(filename: str) -> Optional[str]:
    """Return the `YYYYMMDD_HHMMSS_fff` capture stamp at the end of a dump
    filename, or None if it has none.

    Anchored to the end of the name so a seed that happens to look like a
    date (e.g. `config_20000000_...`) is never mistaken for the stamp. Stamps
    are fixed width, so comparing them as strings compares them in time.
    """
    m = _CAPTURE_STAMP_RE.search(filename)
    return m.group(1) if m else None


class DumpPair(NamedTuple):
    """One GeneratePlacement call: its config dump, its placement dump, the
    seed the game actually used, and the mod's per-session call counter."""
    config: Path
    placement: Path
    seed: int
    call_index: int


class DumpPairing(NamedTuple):
    pairs: list[DumpPair]  # in capture order
    unpaired: dict[Path, str]  # file -> why it could not be paired


_UTC_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?Z")


def _utc_ticks(value: str) -> int:
    """Parse the mod's `capturedAtUtc` (.NET round-trip format, e.g.
    `2026-09-17T09:51:56.0946185Z`) into 100 ns ticks since the Unix epoch.

    Keeps all 7 fractional digits; datetime alone stops at microseconds.
    """
    m = _UTC_RE.fullmatch(value)
    if not m:
        raise ValueError(f"unrecognised capturedAtUtc {value!r}")
    whole = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return int(whole.timestamp()) * 10_000_000 + int((m.group(2) or "").ljust(7, "0"))


def _expected_seed(config: dict) -> Optional[int]:
    """The seed GeneratePlacement will use for this config, or None when the
    game rolls a random one (RandomSeed pref == 1 and randomizeSeed set; see
    re_seed_lifecycle.md section 2 step 5)."""
    prefs = config.get("playerPrefs") or {}
    if prefs.get("RandomSeed") == 1 and config.get("randomizeSeed"):
        return None
    return prefs.get("GameSeed")


def pair_dumps(dumps_dir: Path) -> DumpPairing:
    """Pair every placement dump in `dumps_dir` with its config dump.

    The mod dumps the config on the way into GeneratePlacement and the
    placement on the way out. Files are ordered by their own `capturedAtUtc`
    (100 ns resolution, unaffected by the filename's local-time millisecond
    stamp), and each placement is paired with the config captured last before
    it, provided no other placement lies in between. The pair is then checked
    against its contents: both halves must carry the same `callIndex` (the
    mod's per-session GeneratePlacement counter), and the placement's seed
    must be the one the game would have used for that config.

    Anything that fails is reported in `unpaired` with the reason, never
    paired with a neighbour: a config with no placement after it (an aborted
    call), a placement whose config dump is missing, a callIndex or seed
    disagreement, a wrong/unreadable dump, or a filename without the mod's
    capture stamp.
    """
    dumps_dir = Path(dumps_dir)
    unpaired: dict[Path, str] = {}
    events = []
    for kind, pattern in (("config", "config_*.json"), ("placement", "placement_*.json")):
        for path in dumps_dir.glob(pattern):
            if capture_stamp(path.name) is None:
                unpaired[path] = "filename has no capture stamp"
                continue
            try:
                with open(path) as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                if data.get("dumpType", kind) != kind:
                    raise ValueError(f"dumpType is {data['dumpType']!r}, expected {kind!r}")
                ticks = _utc_ticks(data["capturedAtUtc"])
                call_index = data["callIndex"]
            except (OSError, ValueError, KeyError, TypeError) as e:
                detail = f"missing field {e}" if isinstance(e, KeyError) else str(e)
                unpaired[path] = f"unreadable dump: {detail}"
                continue
            # "config" < "placement" breaks exact ties in the config's favour
            events.append((ticks, kind, path, call_index, data))
    events.sort(key=lambda e: e[:3])

    pairs: list[DumpPair] = []
    pending = None
    for event in events:
        _, kind, path, call_index, data = event
        if kind == "config":
            if pending is not None:
                unpaired[pending[2]] = "no placement captured after it (aborted call?)"
            pending = event
            continue
        if pending is None:
            unpaired[path] = "no config captured before it (config dump missing?)"
            continue
        _, _, config_path, config_call, config = pending
        pending = None
        seed = data.get("seed")
        expected = _expected_seed(config)
        if config_call != call_index:
            reason = (f"callIndex mismatch: {config_path.name} is call {config_call}, "
                      f"{path.name} is call {call_index}")
        elif not isinstance(seed, int):
            reason = f"{path.name} has no integer seed"
        elif expected is not None and seed != expected:
            reason = (f"seed mismatch: {config_path.name} would generate {expected}, "
                      f"{path.name} has {seed}")
        else:
            pairs.append(DumpPair(config_path, path, seed, call_index))
            continue
        unpaired[config_path] = reason
        unpaired[path] = reason
    if pending is not None:
        unpaired[pending[2]] = "no placement captured after it (aborted call?)"
    return DumpPairing(pairs, unpaired)
