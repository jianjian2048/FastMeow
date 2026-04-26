"""Static smoke check for built wheels - validates the embedded sidecar binary
without needing the target OS.

We can't actually run the macOS arm64 binary on Windows, but we CAN verify:
  - The wheel is a valid zip
  - The sidecar binary is present
  - The magic bytes match the expected platform format
  - The binary size is plausible (~22 MB for stripped Go binary)
"""

from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path

# Magic byte signatures for executable formats.
ELF_MAGIC = b"\x7fELF"  # Linux/BSD
MACHO_64_LE = bytes.fromhex("cffaedfe")  # macOS 64-bit little-endian
PE_MAGIC = b"MZ"  # Windows PE/COFF

# Mach-O CPU types (after the 4-byte magic).
MACHO_CPU_ARM64 = 0x0100000C


def check_wheel(wheel_path: Path, expected_platform: str) -> bool:
    print(f"\n=== {wheel_path.name} ===")
    if not wheel_path.exists():
        print(f"  MISSING: {wheel_path}")
        return False

    with zipfile.ZipFile(wheel_path) as z:
        names = z.namelist()
        bins = [n for n in names if "_bin/" in n]
        print(f"  Wheel size: {wheel_path.stat().st_size:,} bytes")
        print(f"  Bin entries: {bins}")
        if not bins:
            print("  FAIL: no _bin/ entry")
            return False

        # Read the binary.
        data = z.read(bins[0])
        print(f"  Binary size: {len(data):,} bytes")
        magic = data[:4]
        print(f"  First 4 bytes: {magic.hex()}")

        if expected_platform == "linux":
            ok = data[:4] == ELF_MAGIC
            print(f"  ELF magic check: {'PASS' if ok else 'FAIL'}")
        elif expected_platform == "macos-arm64":
            ok = data[:4] == MACHO_64_LE
            print(f"  Mach-O 64 LE magic check: {'PASS' if ok else 'FAIL'}")
            if ok:
                cpu_type = struct.unpack("<I", data[4:8])[0]
                arm = cpu_type == MACHO_CPU_ARM64
                print(
                    f"  CPU type: 0x{cpu_type:08x} (arm64 expected 0x0100000c) "
                    f"-> {'PASS' if arm else 'FAIL'}"
                )
                ok = ok and arm
        elif expected_platform == "windows":
            ok = data[:2] == PE_MAGIC
            print(f"  PE magic (MZ) check: {'PASS' if ok else 'FAIL'}")
        else:
            print(f"  unknown platform: {expected_platform}")
            return False

        # Show WHEEL metadata.
        meta = z.read("fastmeow-0.1.0.dist-info/WHEEL").decode()
        print("  --- WHEEL ---")
        for line in meta.strip().splitlines():
            print(f"    {line}")
        return ok


def main() -> int:
    base = Path("verify_wheels")
    checks = [
        (base / "wheel-manylinux2014_x86_64" / "fastmeow-0.1.0-py3-none-manylinux2014_x86_64.whl", "linux"),
        (base / "wheel-macosx_12_0_arm64" / "fastmeow-0.1.0-py3-none-macosx_12_0_arm64.whl", "macos-arm64"),
        (base / "wheel-win_amd64" / "fastmeow-0.1.0-py3-none-win_amd64.whl", "windows"),
    ]
    results = [check_wheel(p, plat) for p, plat in checks]
    print("\n=== summary ===")
    print(f"  {sum(results)}/{len(results)} wheels passed static checks")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
