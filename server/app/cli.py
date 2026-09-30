"""Local commands with isolated read-only source scanning.

Scan imports neither server settings nor the database. Metadata verification
checks a JSON package only and never resumes jobs or dereferences source paths.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_LIMIT_NAMES = (
    "max_files", "max_directory_entries", "max_total_entries", "max_sheets_per_file",
    "max_depth", "max_hash_bytes", "max_total_hash_bytes", "max_table_bytes",
    "max_xlsx_expanded_bytes", "max_rows_per_file", "max_total_rows", "max_columns",
    "max_cell_characters", "max_total_cell_characters", "max_observations", "max_candidate_links",
)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server.app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="read-only local inventory and observations")
    scan.add_argument("root", type=Path)
    scan.add_argument("--output", required=True, type=Path, help="manifest outside source root")
    scan.add_argument("--max-output-bytes", type=_positive_integer, default=32 * 1024 * 1024)
    for name in _LIMIT_NAMES:
        scan.add_argument("--" + name.replace("_", "-"), type=_positive_integer, default=None)
    export = commands.add_parser("metadata-export", help="write a metadata-only database snapshot")
    export.add_argument("output", type=Path)
    export.add_argument("--max-output-bytes", type=_positive_integer, default=32 * 1024 * 1024)
    verify = commands.add_parser("metadata-verify", help="verify metadata hashes and relationships")
    verify.add_argument("input", type=Path)
    verify.add_argument("--max-input-bytes", type=_positive_integer, default=32 * 1024 * 1024)
    return parser


def _emit(value: dict, *, error: bool = False):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=sys.stderr if error else sys.stdout)


def _write_new_json(value: dict, output: Path, *, max_bytes: int):
    """Publish atomically without overwriting a database or existing source file."""
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    if len(payload) > max_bytes:
        raise ValueError(f"Metadata output exceeds size limit ({len(payload)} > {max_bytes})")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists: {output}; choose a new filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ageds-metadata-", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # link() publishes the completed file exclusively; another writer cannot
        # replace our no-clobber check between the check and publication.
        os.link(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "scan":
            from .scanner import ScanLimits, export_manifest, scan_sources
            limits = ScanLimits(**{name: getattr(args, name) for name in _LIMIT_NAMES if getattr(args, name) is not None})
            manifest = scan_sources(args.root, limits=limits)
            output = export_manifest(manifest, args.output, max_bytes=args.max_output_bytes)
            _emit({"command": "scan", "output": str(output), "schema_version": manifest["schema_version"],
                   "coverage": manifest["coverage"], "issue_count": len(manifest["issues"]),
                   "conflict_count": len(manifest["conflicts"]), "source_bytes_written": False})
            # Exit 0 means a manifest was saved. Partial coverage remains explicit
            # in both the manifest and stdout; it is never represented as full.
            return 0
        if args.command == "metadata-export":
            from .packages import export_metadata_package
            package = export_metadata_package()
            _write_new_json(package, args.output, max_bytes=args.max_output_bytes)
            _emit({"command": "metadata-export", "output": str(args.output.resolve()),
                   "schema": package["schema"], "metadata_only": package["metadata_only"],
                   "replay_supported": package["replay_supported"], "signed": package["signed"]})
            return 0
        from .packages import validate_metadata_package
        with args.input.open("rb") as handle:
            raw = handle.read(args.max_input_bytes + 1)
        if len(raw) > args.max_input_bytes:
            raise ValueError("Metadata input exceeds size limit")
        package = json.loads(raw)
        result = validate_metadata_package(package)
        _emit({"command": "metadata-verify", **result})
        return 0 if result["valid"] else 1
    except (OSError, ValueError, RuntimeError) as exc:
        _emit({"command": args.command, "error": type(exc).__name__, "message": str(exc)}, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
