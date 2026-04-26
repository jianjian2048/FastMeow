"""Regenerate protobuf gencode for FastMeow.

Usage:
    python scripts/regen_proto.py

What it does:
    1. Invokes ``grpc_tools.protoc`` against every ``.proto`` under ``proto/``,
       writing outputs into ``src/fastmeow/_generated/``.
    2. Patches the generated ``*_pb2_grpc.py`` files so their internal imports
       use the package layout we actually ship (``fastmeow._generated.*``)
       instead of protoc's default top-level layout (``fastmeow.*``).

Why the patch is necessary:
    ``grpc_tools.protoc`` derives sibling-import paths from the ``.proto``
    package directive (``package fastmeow.v1;``), which produces
    ``from fastmeow.v1 import gateway_pb2``. We ship the gencode under
    ``fastmeow._generated.fastmeow.v1`` to keep it isolated from the public
    package surface, so we rewrite the import once after generation.

Run this whenever ``proto/**/*.proto`` changes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = REPO_ROOT / "proto"
OUT_DIR = REPO_ROOT / "src" / "fastmeow" / "_generated"

# Map protoc's default top-level package paths to our shipped layout.
# Add new entries here if we introduce additional proto packages.
IMPORT_REWRITES: tuple[tuple[str, str], ...] = (
    ("fastmeow.v1", "fastmeow._generated.fastmeow.v1"),
)


def run_protoc() -> None:
    proto_files = sorted(PROTO_DIR.rglob("*.proto"))
    if not proto_files:
        raise SystemExit(f"no .proto files found under {PROTO_DIR}")

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={OUT_DIR}",
        f"--pyi_out={OUT_DIR}",
        f"--grpc_python_out={OUT_DIR}",
        *(str(p) for p in proto_files),
    ]
    print(f"[regen-proto] invoking: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def patch_grpc_imports() -> None:
    grpc_files = sorted(OUT_DIR.rglob("*_pb2_grpc.py"))
    for path in grpc_files:
        original = path.read_text(encoding="utf-8")
        patched = original
        for old_pkg, new_pkg in IMPORT_REWRITES:
            # Match ``from <old_pkg> import ...`` at line start; preserve the rest.
            pattern = re.compile(
                rf"^from {re.escape(old_pkg)} import ", flags=re.MULTILINE
            )
            patched = pattern.sub(f"from {new_pkg} import ", patched)
        if patched != original:
            path.write_text(patched, encoding="utf-8")
            print(f"[regen-proto] patched imports in {path.relative_to(REPO_ROOT)}")
        else:
            print(f"[regen-proto] no import rewrite needed for {path.relative_to(REPO_ROOT)}")


def main() -> None:
    if not OUT_DIR.exists():
        raise SystemExit(f"output dir missing: {OUT_DIR}")
    run_protoc()
    patch_grpc_imports()
    print("[regen-proto] done")


if __name__ == "__main__":
    main()
