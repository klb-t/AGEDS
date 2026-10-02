# On-device ASR prototype

These C++/JNI and CMake sources were preserved from the earlier standalone
Android experiment and moved without byte changes from `androidApp/src/main/cpp`.
The standalone functional UI changes were incorporated in main by `1e71cf8`;
the native ASR sketch was never connected to the application.

It has no Gradle `externalNativeBuild`, bundled whisper dependency, matching
Kotlin native bridge or runtime acceptance. It is not built by the current APK.
Supported transcription currently uses the server-side faster-whisper adapter.

A future experiment must provide that wiring, model acquisition/provenance,
permissions, resource bounds and an actual device acceptance before advertising
on-device transcription. Preserve the previous attempts; do not overwrite source
recordings or substitute a stub result for actual inference.
