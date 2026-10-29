# Runtime dependencies of the production image (2026-10-30)

What each feature the owner can start from the web (or from the CLI inside the container) needs
at run time, and whether the image built by `scripts/deploy_vm.sh` has it.

Sources read: `server/Dockerfile`, `docker-compose.yml`, the VM overlay
`docker-compose.vm.yml` (untracked, present only in the VM checkout), `server/requirements*.txt`,
`scripts/deploy_vm.sh`, `server/app/media/recipes/*.json` and the code that calls each tool.
No image was built and no container was run for this report.

**Legend.** *Read* = taken from the files only. *Local* = checked in the development venv on this
host (`.venv`, Python 3.11.17), which is **not** the image (Python 3.12, other installed set).
A local pass shows the code and the pinned versions work together; it does not prove the image
has them.

## The image in one paragraph

`python:3.12-slim` + Debian `ffmpeg` (brings `ffprobe`) + `pip install -r requirements.txt -r
requirements-whisper.txt`. So: FastAPI stack, `pypdf`, `faster-whisper==1.2.0`, `av==16.1.0`,
`requests`. **Not** installed: `requirements-scanner.txt` (openpyxl, xlrd),
`requirements-align.txt` (torch, torchaudio), pyannote.audio, Tesseract, poppler-utils
(`pdftoppm`, `pdfinfo`), Chromium, gcloud. The container runs as root. Both `api` and
`worker` use this image; ASR/OCR jobs run in `worker`.

## Per feature

| Feature (where started) | Needs at run time | In the image? | How checked |
|---|---|---|---|
| **Default ASR preset `asr-vertex-gemini-aligned`** (owner: `/start` → step 4, `/media/queue`) | Vertex AI Gemini (stdlib `urllib`; token from the GCE metadata server, else `gcloud`), a recorded `vertex` consent, budget; ffmpeg/ffprobe for length and transcoding; local faster-whisper `large-v3-turbo` at revision `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`, CPU int8, downloaded on first use into `HF_HOME` (about 1.6 GB) | Python parts **yes**; model **no** (first job downloads it) | *Read* + *Local*: faster-whisper 1.2.0, ctranslate2 4.8.4, av 16.1.0 import; the pinned revision is in the host's HF cache (1.6 GB) and loads with `local_files_only=True, HF_HUB_OFFLINE=1`; a 2 s synthetic 440 Hz tone transcribed (model load 17 s; the whole run took 132 s while the test suite was using the CPUs, so not a speed figure). Vertex was **not** called. |
| ↳ model cache mount | `HF_HOME` on a persistent volume | VM overlay: **yes**, `HF_HOME=/models` and `/var/lib/ageds-models:/models` for `api` and `worker`. Base `docker-compose.yml`: **no** (`HF_HOME` unset, cache inside the container, lost on recreate) | *Read*. `/var/lib/ageds-models` does **not** exist on this host yet; Docker creates it (root-owned) on `up`. |
| ↳ which preset is the default | The owner's `/settings` choice (`media.default_recipe.asr` / `.ocr` in the database), else `EW_ASR_RECIPE` / `EW_OCR_RECIPE`, else **`legacy`** (faster-whisper `EW_WHISPER_MODEL=small`, no Gemini) and **`ocr-tesseract-pol-nld-eng`** | The VM overlay sets **neither** `EW_ASR_RECIPE` nor `EW_OCR_RECIPE` (`scripts/pilot_local.sh` does) | *Read*. Unless the production database already holds the `/settings` choice, ASR runs `legacy` and OCR runs Tesseract, which is not in the image: **every OCR job fails**. |
| ↳ failure mode of the alignment step | — | — | *Read* (`media/vertex.py`): the paid Gemini call happens first. If faster-whisper then cannot load (no network to huggingface.co, no disk, unwritable `HF_HOME`), the text is kept with `timing_alignment.status = unavailable`: money spent, no word times, no word citations. |
| **Vertex OCR** (`ocr-vertex-gemini`, `ocr-vertex-gemini-chat`) | Vertex as above; `pypdf` for PDF text layers and page counts | **yes** (`pypdf==5.1.0` in `requirements.txt`) | *Read* + *Local*: `pypdf` 5.1.0 imports. |
| **ffmpeg / ffprobe** (transcoding AMR etc. for Gemini, decoded length for the budget, forced alignment input) | `ffmpeg`, `ffprobe` on `PATH` | **yes** (Debian `ffmpeg` package) | *Read*; locally `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` exist (host, not image). Without them: transcoding refused, the budget falls back to bytes/4000. |
| **Forced alignment** `asr-vertex-gemini-forced` (selectable on `/settings`) | `torch==2.5.1`, `torchaudio==2.5.1` (`requirements-align.txt`), MMS_FA weights 1.18 GB in the torch hub cache, ffmpeg | **no** torch/torchaudio | *Read*; locally torch 2.5.1+cpu imports (the venv has the align extras, the image does not). In the image the Gemini call is paid and then alignment is `unavailable` (ImportError): no word times. |
| **Hybrid** `asr-vertex-gemini-hybrid` | as aligned + torch/torchaudio for the fill | partly: primary alignment works, fill **no** | *Read*: the fill is recorded `unavailable`; the faster-whisper alignment stands. |
| **Tesseract presets** `ocr-tesseract-pol-nld-eng`, `ocr-chat-screenshot-pol-nld-eng` | `tesseract` with `pol`, `nld`, `eng` data; `pdftoppm`/`pdfinfo` for PDFs | **no** | *Local*: `python -m server.app.cli media-capabilities` → `ocr_tesseract: unavailable`, `pdf_rasterizer: unavailable` (host has neither either). Also the fallback default OCR preset, see above. |
| **pyannote diarization** `diarization-pyannote` (`kind: diarize`) | `pyannote.audio` 3.x and the gated `pyannote/speaker-diarization-3.1` weights (Hugging Face token) | **no** | *Local*: `media-capabilities` → `diarization_pyannote: unavailable`. |
| **PDF report** | Chromium executable named by `EW_PDF_CHROMIUM` (run with `--no-sandbox` as root) | **no** (no Chromium, variable unset) | *Read* (`reports/outputs.py`): recorded as `unavailable` with the reason; JSON/CSV/HTML still written. Not a crash. |
| **XLSX report output; XLSX/XLS import and plan probes** | `openpyxl`, `xlrd` (`requirements-scanner.txt`) | **no** | *Read*; locally both import. In the image XLSX output is `unavailable`, an uploaded `.xlsx` is not probed (`probe_not_possible`) and stays an original only. |
| **Gmail API connector** (CLI only: `python -m app.cli gmail-…` inside the container; no web route) | stdlib only (`urllib`, `json`, `base64`); OAuth client file and token under `AGEDS_CONFIG_DIR` (default `~/.config/ageds`) | code **yes** | *Read* + *Local* (imports are stdlib). In the container the default is `/root/.config/ageds`, **not on a volume**: the client file must be copied in and the token is lost when the container is recreated. Set `AGEDS_CONFIG_DIR` (or `AGEDS_GMAIL_CLIENT`/`AGEDS_GMAIL_TOKEN`) to a mounted path, e.g. under `/app/data/…`, mode 0600. |
| **Upload → catalogue → plan → import** (`/start`) | stdlib + FastAPI/python-multipart | **yes** | *Local*: the full test suite, including the synthetic owner path in `test_owner_web.py` and `test_real_export_formats.py`. |

## Other build and runtime notes

* **Unpinned transitive packages.** Only direct requirements are pinned. `ctranslate2`,
  `huggingface-hub`, `tokenizers`, `onnxruntime` and the FastAPI/uvicorn extras resolve at build
  time, so the image may differ from the venv the tests ran in (venv: ctranslate2 4.8.4,
  huggingface-hub 2.3.0). *Read.*
* **Python 3.12 in the image vs 3.11.17 in the venv.** Tests have not run on 3.12 here. *Read.*
* **No `.dockerignore`.** `COPY . .` of `./server` copies `server/tests`, `__pycache__` and
  anything placed in `server/data` (tracked: only `.gitkeep`) into the image. *Read.*
* **First ASR job** downloads 1.6 GB from huggingface.co inside the worker; the job runs that long
  longer and needs outbound HTTPS from the container and free space under `/var/lib/ageds-models`.
  Host disk at the time of writing: 41 GB free on `/`. *Local (`df`).*
* **Vertex credentials** come from the GCE metadata server through the Docker bridge. The VM
  service account needs a scope that allows Vertex AI (`cloud-platform`). *Read, not verified.*
* `docker-compose.vm.yml` uses `!override`, which needs Docker Compose ≥ 2.24.

## Would break or silently degrade the deploy

1. **OCR default is Tesseract unless `/settings` or `EW_OCR_RECIPE` says otherwise**, and the image
   has no Tesseract. Every OCR job queued from `/start` step 4 fails.
2. **ASR default is `legacy` (local faster-whisper `small`, no Gemini, no speaker hypotheses) unless
   `/settings` or `EW_ASR_RECIPE` says otherwise.** Not a crash, but not the intended preset.
3. **`asr-vertex-gemini-forced` (and the hybrid fill) can be chosen on `/settings` but cannot work
   in the image**: the Gemini call is paid, word times are `unavailable`.
4. **Gmail connector state is not persisted** in the container.
5. **The base `docker-compose.yml` alone has no model volume**; only the VM overlay does. Deploying
   anywhere without the overlay re-downloads 1.6 GB after each recreate.

## Pre-deploy checklist

- [x] `docker-compose.vm.yml` present in the VM checkout with `HF_HOME=/models` and the
      `/var/lib/ageds-models:/models` mount for **both** `api` and `worker` (it is today).
- [x] Add `EW_ASR_RECIPE: asr-vertex-gemini-aligned` and `EW_OCR_RECIPE: ocr-vertex-gemini` to the
      overlay's environment, **or** right after the deploy open `/settings` and save both presets
      (the stored choice wins over the environment).
- [ ] Do not select `asr-vertex-gemini-forced`, `asr-vertex-gemini-hybrid` (fill), the Tesseract
      presets or diarization in production until the image installs their dependencies.
- [ ] Vertex consent recorded on `/settings` with its basis, provider on, daily budget set.
- [ ] Optional warm-up so the first owner job does not wait for the download: after `up`, inside the
      worker, load the pinned model once
      (`python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8', revision='0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf')"`)
      and check `/var/lib/ageds-models` holds about 1.6 GB.
- [ ] `docker compose … exec worker python -m app.cli media-capabilities` → expect
      `asr_faster_whisper: importable` and the others `unavailable`, as listed above.
- [x] If the Gmail connector will be used: set `AGEDS_CONFIG_DIR` to a mounted private path first.
      Now `/app/data/config`, i.e. `/var/lib/ageds/config` on the host. `deploy_vm.sh` backs up
      only the database and the store, but a whole-directory copy of `/var/lib/ageds` would include
      the Gmail token, which must never reach the backup bucket.
- [ ] Tell the owner: scanned PDFs need Vertex OCR. Since the image update below, PDF and XLSX
      report output and `.xlsx`/`.xls` import work once the new image is built.
- [ ] Then the usual `scripts/deploy_vm.sh` (dry run first, then `--apply`), which runs the test
      suite, stops `api`/`worker`, backs up SQLite and the store, builds and checks.

## Update 2026-10-30, later the same day

* The coordinator changed `docker-compose.vm.yml` (an untracked VM file); checked in the file:
  * it sets `EW_ASR_RECIPE=asr-vertex-gemini-aligned`, `EW_OCR_RECIPE=ocr-vertex-gemini` and
    `AGEDS_CONFIG_DIR=/app/data/config` for `api` and `worker` (shared `&env`);
  * `/var/lib/ageds-models` exists (root-owned; the container runs as root, so it can write there).
* `/settings` and the batch page now check each preset against the installation
  (`owner.preset_status`). Unusable presets cannot be chosen, and their jobs are not queued.
* `server/Dockerfile` now also installs:
  * `requirements-scanner.txt` (openpyxl 3.1.5 / xlrd 2.0.2 locally): XLSX/XLS import, plan
    probes and XLSX report output;
  * Debian `chromium` and `fonts-dejavu-core`, with `EW_PDF_CHROMIUM=/usr/bin/chromium`, for PDF
    report output.

  *Verified locally:* `reports.outputs.write_pdf` with Chromium 141 printed Polish text to a
  valid PDF, and the XLSX import tests pass with openpyxl. *Not verified:* the image build itself
  (no Docker here). The first build on the VM is that check. The image grows by roughly the
  Chromium package and its libraries.
* `scripts/deploy_vm.sh` now **builds before it stops** `api`/`worker`. A failed build (for example
  an apt package name) leaves production running unchanged; revert the Dockerfile hunk and deploy
  again.
* torch/torchaudio for the forced and hybrid presets are still not in the image. The default
  preset does not need them, and `/settings` marks those presets as partial or unavailable.
