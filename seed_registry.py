"""
Read and write Granny Legacy's randomizer seed from Windows registry PlayerPrefs.
"""

import json
import os
import sys
import winreg
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple, Any

# Registry paths
REGISTRY_PATH = r"Software\Omega Mega Gigal Intel\Granny: Legacy"
GAME_SEED_VALUE = "GameSeed_h510350364"
RANDOM_SEED_VALUE = "RandomSeed_h3548222249"

# Track if we've already backed up in this process
_backed_up = False


def _to_signed_int32(unsigned_value: int) -> int:
    """Convert unsigned 32-bit to signed 32-bit.

    Unity stores int as REG_DWORD (unsigned), but PlayerPrefs ints are signed.
    If unsigned > 0x7FFFFFFF, subtract 0x100000000 to recover the signed value.
    """
    if unsigned_value > 0x7FFFFFFF:
        return unsigned_value - 0x100000000
    return unsigned_value


def _to_unsigned_int32(signed_value: int) -> int:
    """Convert signed 32-bit to unsigned 32-bit for registry storage.

    If signed < 0, add 0x100000000 to get the unsigned representation.
    """
    if signed_value < 0:
        return signed_value + 0x100000000
    return signed_value


def read_seed() -> Optional[int]:
    """Read current GameSeed, or None if the value is absent.

    Returns:
        The signed 32-bit seed value, or None if absent.

    Raises:
        FileNotFoundError: If the registry key doesn't exist (game not run yet).
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            try:
                value, _ = winreg.QueryValueEx(key, GAME_SEED_VALUE)
                return _to_signed_int32(value)
            except FileNotFoundError:
                return None
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Registry key not found: HKEY_CURRENT_USER\\{REGISTRY_PATH}\n"
            f"The game has not been run yet."
        )


def read_random_flag() -> Optional[int]:
    """Read RandomSeed flag (0 = use fixed seed, 1 = random).

    Returns:
        0 if fixed seed, 1 if random, None if absent.

    Raises:
        FileNotFoundError: If the registry key doesn't exist.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            try:
                value, _ = winreg.QueryValueEx(key, RANDOM_SEED_VALUE)
                return value
            except FileNotFoundError:
                return None
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Registry key not found: HKEY_CURRENT_USER\\{REGISTRY_PATH}\n"
            f"The game has not been run yet."
        )


def read_all_prefs() -> Dict[str, Tuple[Any, int]]:
    """Read all values under the registry key.

    Returns:
        Dictionary {name: (value, reg_type)} for all prefs.

    Raises:
        FileNotFoundError: If the registry key doesn't exist.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            prefs = {}
            idx = 0
            while True:
                try:
                    name, value, reg_type = winreg.EnumValue(key, idx)
                    prefs[name] = (value, reg_type)
                    idx += 1
                except OSError:
                    # End of enumeration
                    break
            return prefs
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Registry key not found: HKEY_CURRENT_USER\\{REGISTRY_PATH}\n"
            f"The game has not been run yet."
        )


def backup_prefs(path: str) -> None:
    """Dump all registry values to a JSON file for diagnostics/restoration.

    Args:
        path: File path to write the backup JSON.
    """
    prefs = read_all_prefs()
    json_prefs = {}
    for name, (value, reg_type) in prefs.items():
        # Convert bytes to hex string for JSON serialization
        serializable_value = value
        if isinstance(value, bytes):
            serializable_value = value.hex()
        json_prefs[name] = {
            "value": serializable_value,
            "type": reg_type
        }

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, 'w') as f:
        json.dump(json_prefs, f, indent=2)


def restore_prefs(path: str) -> None:
    """Restore all registry values from a JSON backup.

    Args:
        path: File path to read the backup JSON from.
    """
    with open(path, 'r') as f:
        json_prefs = json.load(f)

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, access=winreg.KEY_WRITE) as key:
        for name, data in json_prefs.items():
            value = data["value"]
            reg_type = data["type"]
            # Convert hex string back to bytes for REG_BINARY
            if reg_type == winreg.REG_BINARY and isinstance(value, str):
                value = bytes.fromhex(value)
            winreg.SetValueEx(key, name, 0, reg_type, value)


def write_seed(seed: int, *, also_disable_random: bool = True) -> None:
    """Write GameSeed to registry. Auto-backs up on first write in the process.

    Args:
        seed: Signed 32-bit seed value to write.
        also_disable_random: If True, also set RandomSeed flag to 0 so the
                            game honours the fixed seed.

    Raises:
        ValueError: If seed is outside signed 32-bit range.
        FileNotFoundError: If the registry key doesn't exist.
    """
    global _backed_up

    # Validate seed range
    if seed < -2147483648 or seed > 2147483647:
        raise ValueError(
            f"Seed {seed} is outside signed 32-bit range "
            f"[-2147483648, 2147483647]"
        )

    # Auto-backup on first write in this process
    if not _backed_up:
        backup_dir = Path(__file__).parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"prefs_backup_{timestamp}.json"
        backup_prefs(str(backup_path))
        _backed_up = True

    unsigned_seed = _to_unsigned_int32(seed)

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, access=winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, GAME_SEED_VALUE, 0, winreg.REG_DWORD, unsigned_seed)

            if also_disable_random:
                winreg.SetValueEx(key, RANDOM_SEED_VALUE, 0, winreg.REG_DWORD, 0)
    except FileNotFoundError:
        raise FileNotFoundError(
            f"Registry key not found: HKEY_CURRENT_USER\\{REGISTRY_PATH}\n"
            f"The game has not been run yet."
        )


def main():
    """CLI interface for reading/writing seeds and managing backups."""
    if len(sys.argv) == 1:
        # Print current state
        seed = read_seed()
        flag = read_random_flag()
        all_prefs = read_all_prefs()
        print(f"Current GameSeed: {seed}")
        print(f"RandomSeed flag: {flag}")
        print(f"Total prefs: {len(all_prefs)}")

    elif len(sys.argv) >= 2 and sys.argv[1] == "--set":
        if len(sys.argv) < 3:
            print("Usage: python seed_registry.py --set <int>")
            sys.exit(1)

        try:
            new_seed = int(sys.argv[2])
        except ValueError:
            print(f"Invalid seed: {sys.argv[2]}")
            sys.exit(1)

        old_seed = read_seed()
        print("WARNING: The game must be CLOSED before changing the seed!")
        print("         Unity rewrites all PlayerPrefs from memory on exit")
        print("         and would overwrite the change.")
        print()

        write_seed(new_seed)
        print(f"Seed changed: {old_seed} -> {new_seed}")

    elif len(sys.argv) >= 2 and sys.argv[1] == "--backup":
        if len(sys.argv) < 3:
            print("Usage: python seed_registry.py --backup <path>")
            sys.exit(1)
        backup_prefs(sys.argv[2])
        print(f"Backed up to: {sys.argv[2]}")

    elif len(sys.argv) >= 2 and sys.argv[1] == "--restore":
        if len(sys.argv) < 3:
            print("Usage: python seed_registry.py --restore <path>")
            sys.exit(1)
        restore_prefs(sys.argv[2])
        print(f"Restored from: {sys.argv[2]}")

    else:
        print("Usage:")
        print("  python seed_registry.py                    # Show current state")
        print("  python seed_registry.py --set <int>        # Write new seed")
        print("  python seed_registry.py --backup <path>    # Backup prefs to JSON")
        print("  python seed_registry.py --restore <path>   # Restore prefs from JSON")
        sys.exit(1)


if __name__ == "__main__":
    main()
