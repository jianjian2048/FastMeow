"""Retag a FastMeow wheel for a specific platform.

Usage:
    python scripts/repack_wheel.py <wheel_path> --platform <platform_tag> [--out-dir <dir>]

Example:
    python scripts/repack_wheel.py dist/fastmeow-0.1.0-py3-none-any.whl \\
        --platform manylinux2014_x86_64

What it does:
    1. Unpacks the input wheel.
    2. Edits ``*.dist-info/WHEEL``:
         - ``Tag: py3-none-any``        -> ``Tag: py3-none-<platform>``
         - ``Root-Is-Purelib: true``    -> ``Root-Is-Purelib: false``
       (Because the wheel ships a native Go sidecar binary under
       ``fastmeow/_bin/``, it is platform-dependent and therefore platlib,
       not purelib. See PEP 427 + packaging spec.)
    3. Regenerates ``*.dist-info/RECORD`` with fresh checksums (the WHEEL
       file changed, so its hash must be recomputed).
    4. Repacks the wheel under a platform-tagged filename in --out-dir
       (defaults to the same directory as the input).

Supported platform tags (matched to whatsmeow sidecar cross-compile targets):
    - manylinux2014_x86_64   (linux/amd64,  Go GOOS=linux  GOARCH=amd64)
    - macosx_12_0_arm64      (darwin/arm64, Go GOOS=darwin GOARCH=arm64;
                              floor 12.0 because Go 1.25 dropped macOS 11)
    - win_amd64              (windows/amd64, Go GOOS=windows GOARCH=amd64)

Why a custom script (vs ``wheel tags`` / ``cibuildwheel``):
    * ``wheel tags`` cannot flip ``Root-Is-Purelib``.
    * ``cibuildwheel`` is designed for C-extension wheels and would emit
      cp312/cp313 duplicates with byte-identical content.
    * The wheel is otherwise pure-Python; we only need a platform tag so
      pip resolves the right binary on each OS.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# Whitelist of platform tags we currently support. Adding a new platform here
# must be paired with a matching cross-compile target in the release workflow.
SUPPORTED_PLATFORMS = frozenset(
    {
        "manylinux2014_x86_64",
        "macosx_12_0_arm64",
        "win_amd64",
    }
)


def _hash_and_size(data: bytes) -> tuple[str, int]:
    """Return (sha256-base64-nopad, byte_size) as required by RECORD."""
    digest = hashlib.sha256(data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}", len(data)


def _patch_wheel_metadata(wheel_text: str, platform_tag: str) -> str:
    """Rewrite the WHEEL file: platform tag + Root-Is-Purelib=false.

    Each line is handled independently so we don't depend on field ordering.
    The WHEEL format is "Key: Value" per line (RFC 822-ish).
    """
    out: list[str] = []
    seen_tag = False
    seen_root = False
    for line in wheel_text.splitlines():
        if line.startswith("Tag:"):
            # Replace whatever Tag was emitted (uv_build => "py3-none-any")
            # with our platform-specific tag.
            out.append(f"Tag: py3-none-{platform_tag}")
            seen_tag = True
        elif line.startswith("Root-Is-Purelib:"):
            out.append("Root-Is-Purelib: false")
            seen_root = True
        else:
            out.append(line)
    if not seen_tag:
        raise RuntimeError("WHEEL file is missing a 'Tag:' line; refusing to retag.")
    if not seen_root:
        # Spec says it should always be present, but be defensive.
        out.append("Root-Is-Purelib: false")
    # Preserve trailing newline (many tools rely on it).
    return "\n".join(out) + "\n"


def _regenerate_record(staging: Path, dist_info: Path) -> None:
    """Rewrite RECORD with checksums for every file currently on disk.

    RECORD itself appears with empty hash/size (per spec).
    """
    record_path = dist_info / "RECORD"
    rows: list[tuple[str, str, str]] = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(staging).as_posix()
        if rel == f"{dist_info.name}/RECORD":
            rows.append((rel, "", ""))
            continue
        digest, size = _hash_and_size(path.read_bytes())
        rows.append((rel, digest, str(size)))

    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    for row in rows:
        writer.writerow(row)
    record_path.write_text(buf.getvalue(), encoding="utf-8")


def _derive_output_name(input_name: str, platform_tag: str) -> str:
    """Replace the platform segment of a wheel filename.

    Input:  fastmeow-0.1.0-py3-none-any.whl
    Output: fastmeow-0.1.0-py3-none-<platform_tag>.whl
    """
    if not input_name.endswith(".whl"):
        raise ValueError(f"Not a wheel filename: {input_name}")
    stem = input_name[: -len(".whl")]
    parts = stem.split("-")
    # PEP 427 wheel filename: {dist}-{version}(-{build})?-{python}-{abi}-{platform}
    if len(parts) not in (5, 6):
        raise ValueError(f"Unexpected wheel filename layout: {input_name}")
    parts[-1] = platform_tag
    return "-".join(parts) + ".whl"


def repack(input_wheel: Path, platform_tag: str, out_dir: Path) -> Path:
    if platform_tag not in SUPPORTED_PLATFORMS:
        raise ValueError(
            f"Unsupported platform tag {platform_tag!r}. "
            f"Allowed: {sorted(SUPPORTED_PLATFORMS)}"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = _derive_output_name(input_wheel.name, platform_tag)
    out_path = out_dir / out_name

    with tempfile.TemporaryDirectory(prefix="fastmeow-repack-") as tmp:
        staging = Path(tmp)

        # 1. Unpack
        with zipfile.ZipFile(input_wheel) as zf:
            zf.extractall(staging)

        # 2. Locate the dist-info directory (exactly one expected).
        dist_infos = [p for p in staging.iterdir() if p.is_dir() and p.name.endswith(".dist-info")]
        if len(dist_infos) != 1:
            raise RuntimeError(f"Expected exactly one .dist-info, found {len(dist_infos)}")
        dist_info = dist_infos[0]

        # 3. Patch WHEEL.
        wheel_file = dist_info / "WHEEL"
        wheel_file.write_text(
            _patch_wheel_metadata(wheel_file.read_text(encoding="utf-8"), platform_tag),
            encoding="utf-8",
        )

        # 4. Regenerate RECORD with fresh checksums.
        _regenerate_record(staging, dist_info)

        # 5. Repack. Sort entries for reproducibility.
        # IMPORTANT: zipfile.ZipFile.write() defaults to whatever permissions
        # the host filesystem reports, which is brittle:
        #   - Windows NTFS has no concept of unix +x at all (everything ends
        #     up 0o644 or similar).
        #   - On Linux, `uv build` appears to normalize file modes to 0o644
        #     when it stages files into the wheel, even though our Go build
        #     just produced 0o755. This means the input "py3-none-any" wheel
        #     ALREADY has the sidecar with no +x.
        # Either way, we cannot trust host permissions. We explicitly stamp
        # the sidecar binary as 0o755 and everything else as 0o644 here so
        # the produced wheel is correct regardless of host OS.
        # (Without this, `pip install` on Linux/macOS leaves the sidecar
        # non-executable and the SDK fails with Permission denied at runtime.)
        if out_path.exists():
            out_path.unlink()
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging.rglob("*")):
                if not path.is_file():
                    continue
                arcname = path.relative_to(staging).as_posix()
                # Decide unix mode: sidecar binaries get +x, everything else
                # gets plain 0o644. We match on the `_bin/` path segment so
                # the rule is independent of file extension (Linux/macOS use
                # `fastmeow-sidecar`, Windows uses `fastmeow-sidecar.exe`).
                is_binary = arcname.startswith("fastmeow/_bin/")
                unix_mode = 0o755 if is_binary else 0o644
                # Build a ZipInfo so we can set external_attr (high 16 bits =
                # unix mode). We also set the regular-file marker bit (0o100000)
                # which `pip` checks when restoring permissions.
                info = zipfile.ZipInfo(filename=arcname)
                info.external_attr = (0o100000 | unix_mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                # Preserve mtime determinism: zip out the file with its on-disk
                # mtime (zipfile defaults to current time otherwise, which
                # also breaks reproducibility).
                stat = path.stat()
                info.date_time = time.gmtime(stat.st_mtime)[:6]
                with path.open("rb") as fh:
                    zf.writestr(info, fh.read())

    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Retag a FastMeow wheel for a specific platform.")
    parser.add_argument("wheel", type=Path, help="Path to the input py3-none-any wheel.")
    parser.add_argument(
        "--platform",
        required=True,
        choices=sorted(SUPPORTED_PLATFORMS),
        help="Target platform tag.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: same as input wheel).",
    )
    args = parser.parse_args(argv)

    if not args.wheel.is_file():
        print(f"error: input wheel not found: {args.wheel}", file=sys.stderr)
        return 2

    out_dir = args.out_dir or args.wheel.parent
    out_path = repack(args.wheel, args.platform, out_dir)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
