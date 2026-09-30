from __future__ import annotations
import dataclasses
import importlib.metadata
import math
from numbers import Integral, Real
import platform
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from .config import settings
from .db import init_db, session
from .evidence import sha256_file
from .transcription import word_timing_capabilities
from .jobs import (DEFAULT_LEASE_SECONDS, LeaseLost, claim_job, finish_job,
                   publish_transcript, renew_lease, update_run_metadata)

def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"

def _json_number(value):
    """Project model numeric scalars to the same numeric types persisted by JSON.

    faster-whisper can return numpy.float64 timestamps. Validators deliberately
    require plain JSON numbers; do not let runtime scalar classes falsely mark
    valid words unavailable before the identical values are serialized.
    Preserve bool/None/invalid values so validation never coerces them valid.
    """
    if type(value) in (int, float, bool, type(None)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return float(value)
    return value

@dataclass
class TranscriptionResult:
    text: str
    language: str | None = None
    segments: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

class FasterWhisperAdapter:
    """A replaceable ASR boundary; tests do not load models or use the network."""
    def describe(self) -> dict:
        return {
            "tool": "faster-whisper", "tool_version": _version("faster-whisper"),
            "provider": "local", "model": settings.whisper_model, "model_version": "unknown",
            "parameters": {"device": settings.whisper_device, "compute_type": settings.whisper_compute_type,
                           "word_timestamps": True, "vad_filter": True,
                           "unspecified_options": "library defaults; effective transcription options recorded in result"},
            "metadata": {"python_version": platform.python_version(), "ctranslate2_version": _version("ctranslate2"),
                         "model_revision": "unknown", "seed": "unknown"},
        }

    def transcribe(self, path: str) -> TranscriptionResult:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError("Install requirements-whisper.txt to enable transcription") from e
        model = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                             compute_type=settings.whisper_compute_type)
        segments, info = model.transcribe(path, word_timestamps=True, vad_filter=True)
        out, texts = [], []
        for segment in segments:
            words = [{"start": _json_number(word.start), "end": _json_number(word.end), "word": word.word,
                      "probability": _json_number(word.probability)} for word in (segment.words or [])]
            out.append({"start": _json_number(segment.start), "end": _json_number(segment.end), "text": segment.text, "words": words})
            texts.append(segment.text.strip())
        options = getattr(info, "transcription_options", None)
        if dataclasses.is_dataclass(options):
            options = dataclasses.asdict(options)
        elif hasattr(options, "_asdict"):
            options = dict(options._asdict())
        elif not isinstance(options, dict):
            options = "unknown"
        return TranscriptionResult(" ".join(texts), getattr(info, "language", None), out, {
            "language_probability": _json_number(getattr(info, "language_probability", None)),
            "language_probability_meaning": "language identification probability; not transcript correctness",
            "transcript_confidence": "unknown", "effective_transcription_options": options,
        })

@contextmanager
def _heartbeat(job: dict, lease_seconds: float, interval: float | None = None):
    stop = threading.Event()
    interval = min(60.0, lease_seconds / 3) if interval is None else interval
    if not math.isfinite(interval) or interval <= 0 or interval >= lease_seconds:
        raise ValueError("heartbeat interval must be positive and shorter than lease")
    def beat():
        while not stop.wait(interval):
            try:
                if not renew_lease(job["id"], job["lease_token"], lease_seconds=lease_seconds):
                    break
            except Exception:
                # No successful renewal means no ownership extension. Publication
                # checks the lease independently even after a transient DB error.
                traceback.print_exc()
    thread = threading.Thread(target=beat, name=f"job-heartbeat-{job['id']}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)

def run_transcribe(job: dict, adapter=None) -> int:
    adapter = adapter or FasterWhisperAdapter()
    with session() as db:
        artifact = db.execute("SELECT * FROM artifacts WHERE id=?", (job["artifact_id"],)).fetchone()
        if not artifact:
            raise RuntimeError("artifact missing")
        artifact = dict(artifact)
    path = Path(artifact["stored_path"])
    input_sha256 = sha256_file(path)
    if artifact["sha256"] and artifact["sha256"] != input_sha256:
        raise RuntimeError("stored artifact SHA-256 mismatch; transcription refused")
    metadata = adapter.describe()
    metadata.setdefault("provenance", {}).update({"input_sha256": input_sha256,
        "input_hash_recorded": artifact["sha256"] or "unknown", "source_id": artifact["source_id"],
        "source_locator": artifact["source_locator"] or "unknown"})
    update_run_metadata(job, metadata)
    result = adapter.transcribe(str(path))
    if sha256_file(path) != input_sha256:
        raise RuntimeError("stored artifact changed during transcription; result refused")
    result_metadata = dict(result.metadata)
    result_metadata["transcript_confidence"] = "unknown"
    result_metadata["input_sha256"] = input_sha256
    result_metadata["word_timing"] = word_timing_capabilities(result.segments)
    return publish_transcript(job, text=result.text, model=metadata.get("model") or "unknown", language=result.language,
                              segments=result.segments, metadata=result_metadata)

def finish(job_id: int, status: str, error: str | None = None, *, lease_token: str | None = None) -> bool:
    if not lease_token:
        raise ValueError("lease_token is required to finish a claimed job")
    return finish_job(job_id, lease_token, status, error)

def process_job(job: dict, adapter=None, *, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                heartbeat_interval: float | None = None) -> int:
    try:
        with _heartbeat(job, lease_seconds, heartbeat_interval):
            if job["kind"] != "transcribe":
                raise RuntimeError(f"unknown job kind {job['kind']}")
            return run_transcribe(job, adapter)
    except Exception as error:
        finish(job["id"], "failed", f"{error}\n{traceback.format_exc()}", lease_token=job["lease_token"])
        raise

def main():
    init_db()
    print("Evidence Workbench worker started")
    while True:
        job = claim_job()
        if not job:
            time.sleep(2)
            continue
        try:
            process_job(job)
        except LeaseLost as error:
            print(f"Job {job['id']}: {error}")
        except Exception:
            traceback.print_exc()

if __name__ == "__main__":
    main()
