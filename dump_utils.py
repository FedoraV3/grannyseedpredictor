"""
Shared helper for locating the most recently captured dump file.

Dump filenames are `{name}_{seed}_{timestamp}.json`, where `seed` is a
variable-width number that sorts BEFORE the timestamp. A plain
lexicographic sort on the filename is therefore not chronological order:
e.g. "config_10000_..." sorts before "config_9999_..." because '1' < '9',
even when the 10000 dump was captured
later. Sort by modification time instead.
"""

from pathlib import Path


def newest_dump_file(dumps_dir: Path, pattern: str) -> Path:
    """Return the most recently modified file in `dumps_dir` matching `pattern`.

    Raises FileNotFoundError if no file matches.
    """
    dumps_dir = Path(dumps_dir)
    files = list(dumps_dir.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No files matching {pattern!r} in {dumps_dir}")
    return max(files, key=lambda p: p.stat().st_mtime)
