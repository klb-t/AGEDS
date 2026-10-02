# N46 — native WAV header provenance display

The native source workspace now uses `SourceAudioProjection` for WAV inventory
summaries and file details. The projection reads `ScannedSourceFile.wavHeader`;
it does not open, decode or modify source files. Other formats retain their
existing display path.

The UI separates:

- duration calculated from declared data bytes / header byte rate, rather than a
  duration measured by playback;
- bytes inspected at the beginning of the file, whether the read reached the
  end, and audio samples that were not validated;
- provider-reported size and the total extent declared by the RIFF header;
- an independently recorded whole-file SHA-256, or an explicit statement that no
  whole-file hash was computed. A header observation does not replace that hash.

Malformed, unsupported, inconsistent-size and unknown observation states do not
produce a duration claim. An unknown duration basis, negative or nonfinite value
is also refused for display as a supported header calculation. The newer
observation takes precedence over the legacy `audioDurationSec` field.

Any WAV without `wavHeader` has an explicitly **unknown duration basis and unknown
read coverage**. The UI does not infer an earlier scan: this also covers a fresh
entry that could not be opened or had no remaining scan budget. Its recorded finite
duration can be shown with an unknown basis, but never relabeled as a verified header observation.
The existing cache notice continues to warn that current URI access and current
bytes have not been rechecked. Whole-file hashing still does not establish
identity, authorship, truth, successful playback or acoustic alignment.

## Verification boundary

`SourceAudioDisplayTest` contains eight synthetic JVM tests covering declared
versus legacy duration, bounded-prefix disclosure, independent whole-file hash
and end-of-input, older cache semantics, fresh unopened entries without invented
history, invalid duration/basis/status combinations, other-format isolation, and
contradictory size/counter metadata.

Independent QA executed all eight display tests against the actual source/models
in its direct JVM aggregate: **38 tests passed, 0 failed** in 0.205 seconds
(display plus stream tests). `git diff --check` passed. Integrated Android
compilation remains a separate coordinator gate; this task does not claim device
interaction or media playback.
