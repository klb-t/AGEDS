"""Local commands with isolated read-only source scanning.

Scan imports neither server settings nor the database. Metadata verification
checks a JSON package only and never resumes jobs or dereferences source paths.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
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
    archive_import = commands.add_parser("metadata-archive-import", help="create a NEW inert SQLite metadata archive; never restore live state")
    archive_import.add_argument("input", type=Path)
    archive_import.add_argument("output", type=Path)
    archive_export = commands.add_parser("metadata-archive-export", help="export the exact package from an inert metadata archive")
    archive_export.add_argument("input", type=Path)
    archive_export.add_argument("output", type=Path)
    for command in (archive_import, archive_export):
        command.add_argument("--max-input-bytes", type=_positive_integer, default=32 * 1024 * 1024,
                             help="maximum JSON package bytes (both directions)")
        command.add_argument("--max-archive-bytes", type=_positive_integer, default=128 * 1024 * 1024)
        command.add_argument("--max-rows", type=_positive_integer, default=100_000)
    citation = commands.add_parser("citation-export", help="export one pinned citation and its transcript/provenance as inert metadata")
    citation.add_argument("artifact_id", type=_positive_integer)
    citation.add_argument("anchor_id", type=_positive_integer)
    citation.add_argument("output", type=Path, help="new packet file; existing files are never overwritten")
    inspect = commands.add_parser("citation-inspect", help="independently inspect a bounded citation packet without source access")
    inspect.add_argument("input", type=Path)
    # Accounts and grants (packages P2/P3). Secrets are printed once and stored hashed.
    owner = commands.add_parser("authz-owner-init", help="create the installation owner with an ordinary grant and print one token")
    owner.add_argument("--name", required=True)
    principal = commands.add_parser("authz-principal-add", help="add a principal (recipient, service, agent ...)")
    principal.add_argument("--name", required=True)
    principal.add_argument("--kind", default="unknown")
    token = commands.add_parser("authz-token-issue", help="issue a token for a principal; printed once")
    token.add_argument("--principal-uid", required=True)
    token.add_argument("--label")
    token_revoke = commands.add_parser("authz-token-revoke", help="revoke one token")
    token_revoke.add_argument("--method-uid", required=True)
    grant = commands.add_parser("authz-grant", help="grant capabilities on one case to a principal")
    grant.add_argument("--principal-uid", required=True)
    grant.add_argument("--case-id", required=True, type=_positive_integer)
    grant.add_argument("--capability", action="append", required=True)
    grant.add_argument("--valid-to")
    revoke = commands.add_parser("authz-revoke", help="revoke one grant; copies already taken are not recalled")
    revoke.add_argument("--grant-uid", required=True)
    mode = commands.add_parser("authz-mode", help="set auth.mode (legacy_open or tokens)")
    mode.add_argument("mode")
    commands.add_parser("authz-show", help="list principals, grants and settings without secrets")
    return parser


def _authz_command(args) -> dict:
    from . import auth, authz
    from .db import init_db, session
    init_db()
    with session() as db:
        def principal_id(uid):
            row = db.execute("SELECT id FROM principals WHERE uid=?", (uid,)).fetchone()
            if row is None:
                raise ValueError("principal does not exist")
            return row["id"]
        if args.command == "authz-owner-init":
            result = authz.bootstrap_owner(db, args.name, actor="cli")
            return {**result, "token": auth.issue_token(db, result["principal_id"], label="owner", actor="cli"),
                    "token_shown_once": True}
        if args.command == "authz-principal-add":
            from .assignments import create_principal
            pid = create_principal(db, args.name, kind=args.kind, actor="cli")
            return {"principal_id": pid, "principal_uid": db.execute("SELECT uid FROM principals WHERE id=?", (pid,)).fetchone()["uid"]}
        if args.command == "authz-token-issue":
            return {"token": auth.issue_token(db, principal_id(args.principal_uid), label=args.label, actor="cli"),
                    "token_shown_once": True}
        if args.command == "authz-token-revoke":
            auth.revoke_token(db, args.method_uid, actor="cli")
            return {"revoked": args.method_uid}
        if args.command == "authz-grant":
            if not db.execute("SELECT 1 FROM cases WHERE id=?", (args.case_id,)).fetchone():
                raise ValueError("case does not exist")
            scope = authz.create_scope(db, "case", {"case_id": args.case_id}, actor="cli")
            result = authz.grant(db, principal_id(args.principal_uid), scope, args.capability,
                                 granted_by="cli", valid_to=args.valid_to)
            return {"grant_uid": result["uid"], "warnings": result["warnings"]}
        if args.command == "authz-revoke":
            row = db.execute("SELECT id FROM grants WHERE uid=?", (args.grant_uid,)).fetchone()
            if row is None:
                raise ValueError("grant does not exist")
            authz.revoke_grant(db, row["id"], revoked_by="cli")
            return {"revoked": args.grant_uid}
        if args.command == "authz-mode":
            if args.mode == "tokens":
                # Refuse a switch that would lock the owner out of the HTTP API.
                owner_uid = authz.get_setting(db, "installation.owner_principal_uid")
                owner = db.execute('''SELECT 1 FROM principals p JOIN auth_methods m ON m.principal_id=p.id
                                      WHERE p.uid=? AND m.revoked_at IS NULL''', (owner_uid,)).fetchone()
                if owner is None:
                    raise ValueError("run authz-owner-init first; no owner token exists")
            return authz.set_setting(db, "auth.mode", args.mode, actor="cli")
        return {
            "settings": {r["key"]: json.loads(r["value_json"]) for r in db.execute("SELECT * FROM authz_settings ORDER BY key")},
            "principals": [dict(r) for r in db.execute("SELECT uid,display_name,kind,status,created_at FROM principals ORDER BY id")],
            "grants": [dict(r) for r in db.execute('''SELECT g.uid,p.uid principal_uid,s.kind scope_kind,s.spec_json,
                g.capabilities_json,g.granted_at,g.valid_to,g.revoked_at FROM grants g
                JOIN principals p ON p.id=g.principal_id JOIN scopes s ON s.id=g.scope_id ORDER BY g.id''')],
            "tokens": [dict(r) for r in db.execute('''SELECT m.uid,p.uid principal_uid,m.label,m.created_at,m.revoked_at
                FROM auth_methods m JOIN principals p ON p.id=m.principal_id ORDER BY m.id''')],
        }


def _emit(value: dict, *, error: bool = False):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), file=sys.stderr if error else sys.stdout)


def _write_new_json(value: dict, output: Path, *, max_bytes: int):
    """Publish atomically without overwriting a database or existing source file."""
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    _write_new_bytes(payload, output, max_bytes=max_bytes)


def _write_new_bytes(payload: bytes, output: Path, *, max_bytes: int):
    """Share only the exclusive byte-publication mechanism, not format semantics."""
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
        if args.command.startswith("authz-"):
            _emit({"command": args.command, **_authz_command(args)})
            return 0
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
        if args.command == "citation-export":
            from .exchange import canonical_packet_bytes, export_citation_packet
            packet = export_citation_packet(args.artifact_id, args.anchor_id)
            _write_new_bytes(canonical_packet_bytes(packet), args.output, max_bytes=2 * 1024 * 1024)
            _emit({"command": args.command, "output": str(args.output.resolve()),
                   "schema": packet["schema"], "source_bytes_written": False,
                   "full_pinned_transcript_included": True,
                   "payload_sha256": packet["integrity"]["payload_sha256"]})
            return 0
        if args.command == "citation-inspect":
            from .exchange_consumer import inspect_packet_file
            result = inspect_packet_file(args.input)
            _emit({"command": args.command, **result})
            return 0
        from .archive import ArchiveLimits, load_package
        if args.command in ("metadata-archive-import", "metadata-archive-export"):
            from .archive import import_metadata_archive, export_metadata_archive
            limits = ArchiveLimits(max_input_bytes=args.max_input_bytes,
                                   max_archive_bytes=args.max_archive_bytes, max_rows=args.max_rows)
            if args.command == "metadata-archive-import":
                result = import_metadata_archive(load_package(args.input, limits=limits), args.output, limits=limits)
            else:
                result = export_metadata_archive(args.input, args.output, limits=limits)
            _emit({"command": args.command, "output": str(args.output.resolve()), **result})
            return 0
        from .packages import validate_metadata_package
        package = load_package(args.input, limits=ArchiveLimits(max_input_bytes=args.max_input_bytes))
        result = validate_metadata_package(package)
        _emit({"command": "metadata-verify", **result})
        return 0 if result["valid"] else 1
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        _emit({"command": args.command, "error": type(exc).__name__, "message": str(exc)}, error=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
