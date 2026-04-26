"""In-container smoke test: verify FastMeow wheel installs cleanly and the
embedded sidecar is correctly placed and executable."""
import os
import subprocess
import sys

import fastmeow
from fastmeow._supervisor import _resolve_binary

print(f"python: {sys.version.split()[0]}")
print(f"platform: {sys.platform}")
print(f"fastmeow.__version__: {fastmeow.__version__}")

binary = _resolve_binary(None)
print(f"sidecar path: {binary}")
exists = os.path.isfile(binary)
print(f"sidecar exists: {exists}")
if not exists:
    sys.exit("FAIL: sidecar missing")

size = os.path.getsize(binary)
print(f"sidecar size: {size:,} bytes")

st = os.stat(binary)
exec_bit = bool(st.st_mode & 0o111)
print(f"executable bit: {exec_bit}")
if not exec_bit:
    sys.exit("FAIL: sidecar not executable")

# Verify ELF magic on Linux.
with open(binary, "rb") as f:
    magic = f.read(4)
print(f"first 4 bytes: {magic.hex()}")
if sys.platform.startswith("linux"):
    if magic != b"\x7fELF":
        sys.exit(f"FAIL: not ELF, got {magic!r}")
    print("ELF magic: PASS")

# Verify the binary actually executes in this OS. The sidecar uses Go's
# `flag` package, so -h prints usage to stderr and exits with code 2.
# Either exit 0 or exit 2 with a usage banner means "binary loads & runs".
result = subprocess.run([binary, "-h"], capture_output=True, timeout=10)
print(f"sidecar -h exit: {result.returncode}")
combined = (result.stdout + result.stderr).decode(errors="replace")
print(f"sidecar -h output (first 300 chars): {combined[:300]!r}")
if result.returncode not in (0, 2):
    sys.exit(f"FAIL: unexpected exit code {result.returncode}")
if "Usage of" not in combined and "--listen" not in combined:
    sys.exit("FAIL: -h output does not look like flag usage")
print("sidecar exec check: PASS")

print("=== ALL SMOKE CHECKS PASSED ===")
