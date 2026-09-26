# Private corpus import

AGEDS keeps personal evidence out of this public repository.

For the current case-work workflow, the Android app imports a private `ageds-corpus-seed.json` through Android's Storage Access Framework. The file is copied into app-private storage and then works offline for browsing and selection.

The current private seed contains the normalized communication catalogue derived from the user's Google Sheet: contacts/numbers, SMS/MMS, call log, recording catalogue, institution presets, and known e-mail identities. The seed itself is intentionally not committed.

First-run flow:

1. Install the debug APK.
2. Open **Korpus** and choose `ageds-corpus-seed.json` from Google Drive folder `a`.
3. AGEDS preselects the current priority groups: Gemeente Oss, UWV, Susanne Walstra, Wettbewind, and Acture.
4. Optionally connect the `Recordings` folder using the Android folder picker. AGEDS recursively indexes filenames and matches them against the recording catalogue without changing the source files.
5. With a transcription server configured, **Transkrybuj** uploads only recordings matching the selected corpus and queues them at manual/legal priority.

The public code contains only generic import/selection logic and institution-rule support, never the user's evidence rows.
