"""Durable SQLite leases: each attempt has a run, fenced atomic publication."""
from __future__ import annotations
import json
import math
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from .db import session

DEFAULT_LEASE_SECONDS = 600.0
MAX_LEASE_SECONDS = 86400.0
# A job whose lease expired this many times is failed, not queued again: a file that kills the
# worker would otherwise loop for ever, and with a paid preset each loop reserves another call.
MAX_LEASE_ATTEMPTS = int(os.environ.get("EW_JOB_MAX_ATTEMPTS", "3"))

# Job kinds whose completion is a fenced publication of model output; a bare
# finish_job(..., 'done') cannot complete them without their result.
PUBLISHING_KINDS = ("transcribe", "ocr", "diarize")
PIPELINES = {"transcribe": "AGEDS transcription worker", "ocr": "AGEDS OCR worker",
             "diarize": "AGEDS diarization worker"}

class LeaseLost(RuntimeError):
    """The attempt no longer owns a lease and cannot publish its result."""

def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()

def _clock(now: float | None) -> float:
    value = time.time() if now is None else now
    if not math.isfinite(value):
        raise ValueError("lease clock must be finite")
    return value

def _validate_lease(lease_seconds: float) -> None:
    if not math.isfinite(lease_seconds) or not 0 < lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError("lease_seconds must be finite, positive and at most 86400")

def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)

def _audit(db, action: str, job_id: int, details: dict, actor: str = "worker") -> None:
    db.execute("INSERT INTO audit_log(actor,action,object_kind,object_id,details_json) VALUES (?,?,'job',?,?)",
               (actor, action, job_id, _json(details)))

def _recover_expired(db, now: float) -> int:
    # NULL includes old running jobs: an old worker has no token for publication.
    rows = db.execute("SELECT id,lease_token,attempts FROM jobs WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?)", (now,)).fetchall()
    for row in rows:
        db.execute("UPDATE processing_runs SET status='abandoned',finished_at=?,error=? WHERE lease_token=? AND status='running'",
                   (_timestamp(now), "worker lease expired", row["lease_token"]))
        if row["attempts"] >= MAX_LEASE_ATTEMPTS:
            error = f"worker lease expired on attempt {row['attempts']} of {MAX_LEASE_ATTEMPTS}; not queued again"
            db.execute("UPDATE jobs SET status='failed',lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,worker_id=NULL,finished_at=?,error=? WHERE id=? AND status='running'",
                       (_timestamp(now), error, row["id"]))
            _audit(db, "job_lease_expired", row["id"], {"previous_token": row["lease_token"], "attempts": row["attempts"], "outcome": "failed"})
            continue
        db.execute("UPDATE jobs SET status='queued',lease_token=NULL,lease_expires_at=NULL,heartbeat_at=NULL,worker_id=NULL,started_at=NULL,finished_at=NULL,error=NULL WHERE id=? AND status='running'", (row["id"],))
        _audit(db, "job_lease_expired", row["id"], {"previous_token": row["lease_token"], "attempts": row["attempts"], "outcome": "queued"})
    return len(rows)

def recover_expired_jobs(*, now: float | None = None) -> int:
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        return _recover_expired(db, _clock(now))

def claim_job(*, worker_id: str | None = None, lease_seconds: float = DEFAULT_LEASE_SECONDS, now: float | None = None,
              kinds: tuple[str, ...] | list[str] | None = None) -> dict | None:
    """Claim the next queued job; ``kinds`` restricts a worker to job kinds it can run."""
    _validate_lease(lease_seconds)
    if kinds is not None:
        kinds = tuple(kinds)
        if not kinds or any(not isinstance(kind, str) or not kind for kind in kinds):
            raise ValueError("kinds must be a nonempty sequence of job kind names")
    owner = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        claimed_at = _clock(now)
        _recover_expired(db, claimed_at)
        # Pre-upgrade databases can contain duplicate queued jobs. Preserve them,
        # but never process the same artifact/kind concurrently.
        kind_filter = "" if kinds is None else " AND j.kind IN (%s)" % ",".join("?" * len(kinds))
        row = db.execute("SELECT j.* FROM jobs j WHERE j.status='queued'" + kind_filter + " AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.status='running' AND r.kind=j.kind AND r.artifact_id IS j.artifact_id) ORDER BY j.priority DESC,j.id LIMIT 1",
                         kinds or ()).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        db.execute("UPDATE jobs SET status='running',attempts=attempts+1,started_at=?,finished_at=NULL,error=NULL,lease_token=?,lease_expires_at=?,heartbeat_at=?,worker_id=? WHERE id=? AND status='queued'",
                   (_timestamp(claimed_at), token, claimed_at + lease_seconds, _timestamp(claimed_at), owner, row["id"]))
        provenance = {"artifact_id": row["artifact_id"], "job_id": row["id"], "attempt": row["attempts"] + 1, "worker_id": owner,
                      "pipeline": PIPELINES.get(row["kind"], "AGEDS worker"), "pipeline_version": "unknown"}
        run = db.execute("INSERT INTO processing_runs(job_id,artifact_id,lease_token,status,started_at,provenance_json) VALUES (?,?,?,'running',?,?)",
                         (row["id"], row["artifact_id"], token, _timestamp(claimed_at), _json(provenance)))
        job = dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
        job["run_id"] = int(run.lastrowid)
        _audit(db, "job_claimed", row["id"], {"run_id": job["run_id"], "attempt": job["attempts"], "lease_token": token, "lease_expires_at": job["lease_expires_at"]}, owner)
        return job

def _owned(db, job_id: int, token: str, now: float):
    return db.execute("SELECT * FROM jobs WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at>?", (job_id, token, now)).fetchone()

def renew_lease(job_id: int, token: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS, now: float | None = None) -> bool:
    _validate_lease(lease_seconds)
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        heartbeat = _clock(now)
        if not _owned(db, job_id, token, heartbeat):
            return False
        db.execute("UPDATE jobs SET heartbeat_at=?,lease_expires_at=? WHERE id=? AND lease_token=?", (_timestamp(heartbeat), heartbeat + lease_seconds, job_id, token))
        return True

def update_run_metadata(job: dict, metadata: dict, *, now: float | None = None) -> None:
    """Record known values before inference; absent/empty model fields are unknown."""
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        if not _owned(db, job["id"], job["lease_token"], _clock(now)):
            raise LeaseLost(f"job {job['id']} lease lost")
        run = db.execute("SELECT * FROM processing_runs WHERE id=? AND lease_token=? AND status='running'", (job["run_id"], job["lease_token"])).fetchone()
        if run is None:
            raise LeaseLost("processing run no longer active")
        provenance = json.loads(run["provenance_json"])
        provenance.update(metadata.get("provenance") or {})
        db.execute("UPDATE processing_runs SET tool=?,tool_version=?,provider=?,model=?,model_version=?,parameters_json=?,provenance_json=?,metadata_json=? WHERE id=? AND lease_token=?",
                   tuple(metadata.get(field) or "unknown" for field in ("tool", "tool_version", "provider", "model", "model_version")) +
                   (_json(metadata.get("parameters") or {}), _json(provenance), _json(metadata.get("metadata") or {}), job["run_id"], job["lease_token"]))

def publish_result(job: dict, write, *, now: float | None = None, summary: dict | None = None):
    """Run ``write(db, current_job_row)`` and complete the job in one fenced transaction.

    Only the owner of the unexpired lease and its running processing run can
    publish; any exception inside ``write`` rolls back the output, FTS rows,
    audit entries and the completion together.
    """
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        finished_at = _clock(now)
        current = _owned(db, job["id"], job["lease_token"], finished_at)
        if current is None:
            raise LeaseLost(f"job {job['id']} lease lost; result was not published")
        run = db.execute("SELECT id FROM processing_runs WHERE id=? AND job_id=? AND lease_token=? AND status='running'", (job["run_id"], job["id"], job["lease_token"])).fetchone()
        if run is None:
            raise LeaseLost("processing run no longer active")
        if current["artifact_id"] != job["artifact_id"]:
            raise ValueError("job artifact does not match current lease")
        result = write(db, current)
        db.execute("UPDATE processing_runs SET status='done',finished_at=? WHERE id=?", (_timestamp(finished_at), job["run_id"]))
        db.execute("UPDATE jobs SET status='done',finished_at=?,lease_expires_at=NULL WHERE id=? AND lease_token=?", (_timestamp(finished_at), job["id"], job["lease_token"]))
        details = {"run_id": job["run_id"]}
        details.update(summary(result) if callable(summary) else (summary or {}))
        _audit(db, "job_completed", job["id"], details, current["worker_id"] or "worker")
        return result

def publish_derived_text(job: dict, *, kind: str, text: str, model: str | None = None, language: str | None = None,
                         segments: list | None = None, metadata: dict | None = None, now: float | None = None,
                         also=None) -> int:
    """Fenced append of one derived_text version; ``also(db, derived_text_id)`` adds linked rows."""
    from .evidence import add_derived_text
    def write(db, current):
        did = add_derived_text(int(current["artifact_id"]), kind, text, model=model, language=language, segments=segments,
                               confidence=None, run_id=job["run_id"], metadata=metadata or {}, db=db)
        if also is not None:
            also(db, did)
        return did
    return publish_result(job, write, now=now, summary=lambda did: {"derived_text_id": did})

def publish_transcript(job: dict, *, text: str, model: str | None = None, language: str | None = None,
                       segments: list | None = None, metadata: dict | None = None, now: float | None = None) -> int:
    return publish_derived_text(job, kind="transcript", text=text, model=model, language=language,
                                segments=segments, metadata=metadata, now=now)

def finish_job(job_id: int, token: str, status: str, error: str | None = None, *, now: float | None = None) -> bool:
    if status not in {"failed", "done"}:
        raise ValueError("finish status must be failed or done")
    with session() as db:
        db.execute("BEGIN IMMEDIATE")
        finished_at = _clock(now)
        current = _owned(db, job_id, token, finished_at)
        if current is None:
            return False
        if status == "done" and current["kind"] == "transcribe":
            raise ValueError("transcription completion requires publish_transcript")
        if status == "done" and current["kind"] in PUBLISHING_KINDS:
            raise ValueError(f"{current['kind']} completion requires a fenced publication of its result")
        db.execute("UPDATE processing_runs SET status=?,error=?,finished_at=? WHERE lease_token=? AND status='running'", (status, error, _timestamp(finished_at), token))
        db.execute("UPDATE jobs SET status=?,error=?,finished_at=?,lease_expires_at=NULL WHERE id=? AND lease_token=?", (status, error, _timestamp(finished_at), job_id, token))
        _audit(db, "job_failed" if status == "failed" else "job_completed", job_id, {"lease_token": token, "error": error}, current["worker_id"] or "worker")
        return True
