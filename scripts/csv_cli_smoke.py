#!/usr/bin/env python3
"""Independent, synthetic CSV physical-record acceptance through the actual CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def snapshot(root):
    return {str(p.relative_to(root)): {"sha256": sha(p.read_bytes()),
            "size_bytes": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
            for p in sorted(root.rglob("*")) if p.is_file()}


def fixtures():
    # Expectations are authored here, not obtained with csv.reader/splitlines.
    controls = "A\u2028B\u2029C\u0085D\vE\fF\x1cG\x1dH\x1eI"
    multiline = "x\rY\nZ\r\nQ"
    for name, encoding, bom, delimiter, preamble in (
        ("utf8.csv", "utf-8", b"", ";", "sep=;\r"),
        ("utf16le.csv", "utf-16-le", b"\xff\xfe", ";", "sep=;\r"),
        ("utf8bom.tsv", "utf-8", b"\xef\xbb\xbf", "\t", ""),
        ("utf16be.tsv", "utf-16-be", b"\xfe\xff", "\t", ""),
    ):
        d = delimiter
        records = [f"key{d}payload\r\n", f"plain{d}{controls}\r",
                   f'multi{d}"{multiline}"\n', f'quoted{d}"{controls}"\r\n',
                   f'final{d}"quote ""kept""{d}inside"']
        values = [["key", "payload"], ["plain", controls], ["multi", multiline],
                  ["quoted", controls], ["final", f'quote "kept"{d}inside']]
        offset = bool(preamble)
        yield name, bom + (preamble + "".join(records)).encode(encoding), {
            "encoding": "utf-16" if encoding.startswith("utf-16") else "utf-8-sig",
            "delimiter": d, "preamble": preamble or None, "records": records,
            "values": values, "start_lines": [n + offset for n in (1, 2, 3, 7, 8)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    fingerprint_paths = ["server/app/scanner.py", "server/app/cli.py", "server/app/wav_header.py",
                         "scripts/csv_cli_smoke.py"]
    code = {p: sha((REPO / p).read_bytes()) for p in fingerprint_paths}
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    runs = []
    with tempfile.TemporaryDirectory(prefix="ageds-csv-cli-") as temporary:
        work = Path(temporary)
        source, metadata = work / "source", work / "metadata"
        source.mkdir()
        expected = {}
        for name, raw, spec in fixtures():
            (source / name).write_bytes(raw)
            expected[name] = spec
        before = snapshot(source)
        for label, extra in (("complete", []), ("limited", ["--max-rows-per-file", "2"])):
            output = metadata / f"{label}.json"
            command = [sys.executable, "-m", "server.app.cli", "scan", str(source),
                       "--output", str(output), *extra]
            proc = subprocess.run(command, cwd=source, env=env, capture_output=True,
                                  text=True, timeout=30)
            assert proc.returncode == 0, (command, proc.stdout, proc.stderr)
            status = json.loads(proc.stdout)
            manifest_bytes = output.read_bytes()
            manifest = json.loads(manifest_bytes)
            complete = label == "complete"
            assert status["coverage"]["complete"] is complete
            assert manifest["coverage"]["complete"] is complete
            assert status["source_bytes_written"] is False
            assert manifest["policy"]["read_only"] is True
            assert manifest["policy"]["source_copies"] is False
            assert not output.is_relative_to(source)
            files = {f["file_id"]: f for f in manifest["files"]}
            assert len(files) == len(expected) == len(manifest["tables"])
            checked = 0
            for table in manifest["tables"]:
                file = files[table["file_id"]]
                name = file["relative_path"]
                spec = expected[name]
                assert file["sha256"] == before[name]["sha256"]
                assert file["mtime_ns"] == before[name]["mtime_ns"]
                assert file["hash_status"] == "complete"
                assert table["complete"] is complete
                assert table["encoding"] == spec["encoding"]
                assert table["delimiter"] == spec["delimiter"]
                assert table["preamble"] == spec["preamble"]
                count = 5 if complete else 2
                assert len(table["rows"]) == count
                for index, row in enumerate(table["rows"]):
                    assert row["row"] == index + 1
                    assert row["start_line"] == spec["start_lines"][index]
                    assert row["raw_record"] == spec["records"][index]
                    assert row["raw_record_complete"] is True
                    assert [c["value"] for c in row["cells"]] == spec["values"][index]
                    assert all(c["complete"] for c in row["cells"])
                    checked += 1
            if complete:
                assert not manifest["issues"], manifest["issues"]
            else:
                assert sum(i["code"] == "row_limit" for i in manifest["issues"]) == 4
            assert snapshot(source) == before
            runs.append({"name": label, "command": command, "exit_code": proc.returncode,
                         "stderr": proc.stderr, "records_checked": checked,
                         "coverage_complete": complete, "manifest_sha256": sha(manifest_bytes),
                         "manifest_bytes": len(manifest_bytes), "source_snapshot_unchanged": True,
                         "metadata_outside_source": True})
        after = snapshot(source)
        assert sorted(p.name for p in work.iterdir()) == ["metadata", "source"]
    assert code == {p: sha((REPO / p).read_bytes()) for p in fingerprint_paths}
    receipt = {"schema": "ageds.csv-cli-smoke/v1", "executed_at": datetime.now(timezone.utc).isoformat(),
               "python": sys.version, "code_sha256": code, "code_unchanged_during_run": True,
               "fixtures_before": before, "fixtures_after": after, "runs": runs,
               "scope": "Synthetic CLI CSV/TSV decoding and metadata export; no private corpus, SAF or ASR claim."}
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": True, "runs": len(runs), "records_checked": sum(r["records_checked"] for r in runs)}))


if __name__ == "__main__":
    main()
