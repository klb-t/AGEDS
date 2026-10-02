# ASR: fresh installation and verified-reader acceptance

**PASS — 2026-10-02.** This closes the previously unexecuted clean-install check
`AGEDS-20261001-N17`. A new virtual environment outside the checkout installed
`server/requirements.txt` and `server/requirements-whisper.txt` with
`--no-cache-dir`; all **nine direct pins**, **14 runtime imports**, and
`pip check` passed. Every imported package came from this new environment;
system site packages were disabled. The checkout's backend `.venv` was untouched.

The complete installation log, resolved packages, requirement hashes, model
acquisition observations, command and new smoke receipt are retained in
[ASR_COMPLETION_2026-10-02.json](ASR_COMPLETION_2026-10-02.json).
Historical receipts remain unchanged under
[`../archive/2026-09-30_2026-10-01/`](../archive/2026-09-30_2026-10-01/).
The verified smoke now reads its historical model-hash baseline from that archive.

## Additional real inference

At **20:30 UTC**, the same new environment completed actual tiny.en CPU/int8
inference through the production queue, worker and `VerifiedReader`, with
`HF_HUB_OFFLINE=1` during inference. The model was acquired beforehand from
`Systran/faster-whisper-tiny.en`, pinned revision
`0d3d19a32d3338f10357c0889762bd8d64bbdeba`. All four downloaded file hashes match
the accepted historical N11 model bytes. No paid service or private corpus was used.

| Check | Observed result |
|---|---|
| Input | A new FFmpeg/flite English utterance, 5.43 seconds |
| Decoder input | Production `VerifiedReader`; 10 actual reads of the same object |
| Integrity | Initial hash and final retained-descriptor verification passed; descriptor closed |
| Stored result | One segment, 17 saved words; job completed with publication |
| Exact citation | ` The quick brown`, transcript 1, 0–1020 ms |
| Metadata package | Validation and exact canonical inert SQLite roundtrip passed |
| Preservation | Generated source hash/mtime and model weight hash unchanged |
| Runtime | 4.241 seconds worker; 5.004 seconds harness |

Raw ASR output remains:

> The quick brown fox jumps over the lazy dog, this is a test of locals' peach recognition.

The synthesis input says “local speech recognition.” The recognition error remains
in the saved result. This checks execution and downstream contracts; it does not
measure recognition quality on human, Polish, noisy or multi-speaker audio, verify
acoustic alignment, or establish transcript correctness. There was no manual
listening. The inert archive contains metadata, without media/model bytes, replay,
signature or live restore. Runtime includes model loading and profiling.

## Reproduce

Use a new environment and a new output directory. FFmpeg must provide the `flite`
filter and `slt` voice. This run used Python **3.12.14**, Linux x86_64 and FFmpeg
**6.1.1**. Other platform/resolver combinations were not tested. Scanner and test
extras are outside this environment's install scope.

```sh
python3 -m venv /path/to/new-asr-venv
/path/to/new-asr-venv/bin/python -m pip install --no-cache-dir \
  -r server/requirements.txt -r server/requirements-whisper.txt
/path/to/new-asr-venv/bin/python -m pip check
```

Acquire `config.json`, `tokenizer.json`, `vocabulary.txt` and `model.bin` outside
the checkout from the pinned revision, using this URL pattern:

```text
https://huggingface.co/Systran/faster-whisper-tiny.en/resolve/0d3d19a32d3338f10357c0889762bd8d64bbdeba/{filename}
```

Then execute the existing harness:

```sh
/path/to/new-asr-venv/bin/python scripts/verified_asr_smoke.py \
  --model-dir /path/to/local/tiny.en \
  --model-revision 0d3d19a32d3338f10357c0889762bd8d64bbdeba \
  --work-dir /path/to/new-isolated-output
```

Success requires exit code 0 and `status: passed`, including downstream acceptance.
The reused harness's internal historical task label is N39; the enclosing new
receipt identifies this invocation as additional N17 evidence. Transitive versions
are recorded as the resolver snapshot; the repository pins only its declared
direct requirements. A future resolver can choose different transitive versions.
