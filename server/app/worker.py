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
from typing import BinaryIO
from .config import settings
from .db import init_db, session
from .verified_reader import CHUNK_SIZE, MAX_MEDIA_BYTES, MAX_READ_BYTES, VerifiedReader
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
    # Measured amount of input processed, e.g. {"unit": "audio_second", "amount": 12.5,
    # "basis": "..."}; recorded in run metadata for queue estimates, never in text.
    work: dict | None = None

def _options_dict(options):
    if dataclasses.is_dataclass(options):
        return dataclasses.asdict(options)
    if hasattr(options, "_asdict"):
        return dict(options._asdict())
    return options if isinstance(options, dict) else "unknown"

def _duration_work(info, basis: str) -> dict | None:
    duration = _json_number(getattr(info, "duration", None))
    if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
        return {"unit": "audio_second", "amount": duration, "basis": basis}
    return None

class FasterWhisperAdapter:
    """A replaceable ASR boundary; tests do not load models or use the network.

    ``recipe`` is a record from ``media.recipe`` (strategy ``auto_per_file``).
    Without it the adapter keeps the pre-recipe call: settings model/device/
    compute type and ``transcribe(audio, word_timestamps=True, vad_filter=True)``,
    which is also exactly what the ``legacy`` preset produces.
    """
    def __init__(self, recipe: dict | None = None):
        if recipe is not None and recipe["effective"].get("strategy") != "auto_per_file":
            raise ValueError("FasterWhisperAdapter runs only the auto_per_file strategy")
        self.recipe = recipe

    def _call(self):
        if self.recipe is None:
            return ((settings.whisper_model,), {"device": settings.whisper_device, "compute_type": settings.whisper_compute_type},
                    {"word_timestamps": True, "vad_filter": True})
        from .media.recipe import auto_per_file_transcribe_kwargs
        effective = self.recipe["effective"]
        return ((effective["model"],), {"device": effective["device"], "compute_type": effective["compute_type"],
                                        **effective["model_options"]},
                auto_per_file_transcribe_kwargs(effective))

    def describe(self) -> dict:
        if self.recipe is not None:
            (model,), model_kwargs, call = self._call()
            effective = self.recipe["effective"]
            parameters = {"device": effective["device"], "compute_type": effective["compute_type"],
                          "word_timestamps": call["word_timestamps"], "vad_filter": call["vad_filter"],
                          "strategy": "auto_per_file", "transcribe_kwargs": call,
                          "model_options": effective["model_options"],
                          "unspecified_options": "library defaults; effective transcription options recorded in result",
                          "recipe_id": self.recipe["id"], "recipe_sha256": self.recipe["sha256"],
                          "recipe": effective}
            return {"tool": "faster-whisper", "tool_version": _version("faster-whisper"), "provider": "local",
                    "model": model, "model_version": "unknown", "parameters": parameters,
                    "metadata": {"python_version": platform.python_version(), "ctranslate2_version": _version("ctranslate2"),
                                 "model_revision": effective["model_options"].get("revision", "unknown"), "seed": "unknown"}}
        return {
            "tool": "faster-whisper", "tool_version": _version("faster-whisper"),
            "provider": "local", "model": settings.whisper_model, "model_version": "unknown",
            "parameters": {"device": settings.whisper_device, "compute_type": settings.whisper_compute_type,
                           "word_timestamps": True, "vad_filter": True,
                           "unspecified_options": "library defaults; effective transcription options recorded in result"},
            "metadata": {"python_version": platform.python_version(), "ctranslate2_version": _version("ctranslate2"),
                         "model_revision": "unknown", "seed": "unknown"},
        }

    def transcribe(self, audio: str | BinaryIO) -> TranscriptionResult:
        """Exhaust ASR while the caller owns audio's lifetime.

        Low-level direct callers may still supply a path for compatibility. The
        worker always supplies a verified seekable stream, never a pathname.
        """
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError("Install requirements-whisper.txt to enable transcription") from e
        model_args, model_kwargs, call = self._call()
        model = WhisperModel(*model_args, **model_kwargs)
        segments, info = model.transcribe(audio, **call)
        out, texts = [], []
        for segment in segments:
            words = [{"start": _json_number(word.start), "end": _json_number(word.end), "word": word.word,
                      "probability": _json_number(word.probability)} for word in (segment.words or [])]
            out.append({"start": _json_number(segment.start), "end": _json_number(segment.end), "text": segment.text, "words": words})
            texts.append(segment.text.strip())
        options = _options_dict(getattr(info, "transcription_options", None))
        return TranscriptionResult(" ".join(texts), getattr(info, "language", None), out, {
            "language_probability": _json_number(getattr(info, "language_probability", None)),
            "language_probability_meaning": "language identification probability; not transcript correctness",
            "transcript_confidence": "unknown", "effective_transcription_options": options,
        }, _duration_work(info, "faster_whisper_info_duration_of_decoded_audio"))

def adapter_for_recipe(record: dict):
    """Build the adapter that executes a pinned recipe record."""
    strategy = record["effective"].get("strategy")
    if strategy == "auto_per_file":
        return FasterWhisperAdapter(record)
    if strategy == "per_segment_language":
        from .media.strategies import CodeSwitchingAdapter
        return CodeSwitchingAdapter(record)
    raise RuntimeError(f"no adapter for ASR strategy {strategy!r}")

def recipe_application(adapter, record: dict, built: bool) -> str:
    if built:
        return "adapter_built_from_pinned_recipe"
    bound = getattr(adapter, "recipe", None)
    if isinstance(bound, dict) and bound.get("sha256") == record["sha256"]:
        return "injected_adapter_bound_to_same_recipe"
    return "injected_adapter_not_bound_to_recipe"

def _wav_work(audio) -> dict | None:
    """Declared WAV duration from the verified stream's header, after decoding."""
    from .wav_header import MAX_PREFIX_BYTES, probe_wav_header
    try:
        audio.seek(0)
        prefix = audio.read(MAX_PREFIX_BYTES)
        probe = probe_wav_header(prefix, audio.seek(0, 2))
    except Exception:
        return None
    duration = probe.get("declared_duration_sec")
    if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
        return {"unit": "audio_second", "amount": duration, "basis": "wav_header_declared_duration"}
    return None

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
    from .media.recipe import recipe_from_job, run_recipe_record
    record = recipe_from_job(job, settings=settings)
    built = adapter is None
    adapter = adapter or adapter_for_recipe(record)
    with session() as db:
        artifact = db.execute("SELECT * FROM artifacts WHERE id=?", (job["artifact_id"],)).fetchone()
        if not artifact:
            raise RuntimeError("artifact missing")
        artifact = dict(artifact)
    metadata = adapter.describe()
    verification = {
        "initial_full_sha256": "not_completed",
        "final_same_descriptor": "not_performed",
        "read_policy": "verify_covering_chunks_before_return",
        "input_mode": "verified_seekable_filelike",
        "decoder_read_coverage": "not_measured",
        "limits": {"max_media_bytes": MAX_MEDIA_BYTES, "max_read_bytes": MAX_READ_BYTES,
                   "verification_chunk_bytes": CHUNK_SIZE},
        "worker_path_reopen": False,
        "worker_media_copy": False,
        "limitations": [
            "byte identity does not establish authenticity, alignment or transcript accuracy",
            "same descriptor and verified read buffers are not filesystem immutability",
            "adapter is trusted to use the supplied stream; input consumption is not attested",
        ],
    }
    provenance = metadata.setdefault("provenance", {})
    provenance["recipe"] = run_recipe_record(record, recipe_application(adapter, record, built))
    provenance.update({"input_hash_recorded": artifact["sha256"] or "unknown",
        "input_size_recorded": artifact["size_bytes"], "source_id": artifact["source_id"],
        "source_locator": artifact["source_locator"] or "unknown",
        "input_mode": "verified_seekable_filelike", "input_verification": verification})
    update_run_metadata(job, metadata)
    # Retain one descriptor across decoding and lazy segment consumption. There
    # is deliberately no pathname, temporary-file, copy or unverified fallback.
    with VerifiedReader(artifact["stored_path"], artifact["sha256"], artifact["size_bytes"],
                        store_root=settings.store_dir) as audio:
        verification["initial_full_sha256"] = "passed"
        provenance["input_sha256"] = artifact["sha256"]
        update_run_metadata(job, metadata)
        adapter_started = time.monotonic()
        result = adapter.transcribe(audio)
        adapter_seconds = time.monotonic() - adapter_started
        try:
            audio.verify_unchanged()
        except Exception:
            verification["final_same_descriptor"] = "failed"
            update_run_metadata(job, metadata)
            raise
        verification["final_same_descriptor"] = "passed"
        work = getattr(result, "work", None) or _wav_work(audio) or {"unit": "audio_second", "amount": "unknown",
                                                                     "basis": "not_reported_by_adapter_or_header"}
        work = dict(work, adapter_wall_seconds=adapter_seconds,
                    estimate_note="run wall time (started_at..finished_at) includes model load and verification")
        run_metadata = metadata.get("metadata")
        metadata["metadata"] = dict(run_metadata) if isinstance(run_metadata, dict) else {}
        metadata["metadata"]["work"] = work
        update_run_metadata(job, metadata)
        result_metadata = dict(result.metadata)
        result_metadata["transcript_confidence"] = "unknown"
        result_metadata["input_sha256"] = artifact["sha256"]
        result_metadata["input_mode"] = "verified_seekable_filelike"
        result_metadata["input_verification"] = verification
        result_metadata["word_timing"] = word_timing_capabilities(result.segments)
        requested = record["effective"].get("word_timestamps")
        result_metadata["recipe"] = {"id": record["id"], "sha256": record["sha256"],
                                     "pin_status": record.get("pin_status", "unknown"),
                                     "application": provenance["recipe"]["application"],
                                     "word_timestamps_requested": requested}
        if requested is False:
            result_metadata["word_timing"]["recipe_word_timestamps"] = (
                "disabled by recipe; citations of this version are limited to segment selectors")
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
            if job["kind"] == "transcribe":
                return run_transcribe(job, adapter)
            if job["kind"] == "ocr":
                from .media.ocr import run_ocr
                return run_ocr(job, adapter)
            if job["kind"] == "diarize":
                from .media.diarization import run_diarize
                return run_diarize(job, adapter)
            if job["kind"] == "import_plan_item":
                from .importplan.execute import run_queued_job
                return run_queued_job(job)
            raise RuntimeError(f"unknown job kind {job['kind']}")
    except Exception as error:
        finish(job["id"], "failed", f"{error}\n{traceback.format_exc()}", lease_token=job["lease_token"])
        raise

def worker_kinds() -> tuple[str, ...] | None:
    """EW_WORKER_KINDS (comma-separated, e.g. "transcribe,ocr") limits what this worker claims."""
    import os
    kinds = tuple(kind.strip() for kind in os.getenv("EW_WORKER_KINDS", "").split(",") if kind.strip())
    return kinds or None

def main():
    init_db()
    kinds = worker_kinds()
    print("Evidence Workbench worker started" + (f" for job kinds {', '.join(kinds)}" if kinds else ""))
    while True:
        job = claim_job(kinds=kinds)
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
