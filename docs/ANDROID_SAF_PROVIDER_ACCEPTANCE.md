# SAF provider acceptance — N10

This environment has **no connected Android runtime**. `/dev/kvm` and the KVM
kernel module are absent; the recovered SDK has neither an emulator binary nor
system images. `adb devices -l` returned an empty device list. No image was
downloaded and no emulator, phone, picker or source-provider interaction is
claimed. The receipt records the capability probe separately from compilation.

**Compilation passed:** `:androidApp:assembleDebugAndroidTest` executed 60 tasks
in 2 min 13 s and produced a 407,235-byte test APK. All six test methods and the
provider compiled. None ran on Android. The test inputs match the recorded
snapshot; concurrent XLS/OOXML edits changed two application files afterward,
so this is not the final integrated application build. See
[the machine-readable receipt](archive/2026-09-30_2026-10-01/ANDROID_SAF_PROVIDER_RECEIPT.json).

Six instrumentation contracts are provided in
`androidApp/src/androidTest/java/dev/klbt/ageds/SourceScannerProviderTest.kt`:

1. Seedless directory scan preserves distinct URI identities, duplicate names,
   raw invalid date text, complete-file hashes and original fixture bytes.
2. A provider throwing a permission exception yields explicit partial coverage.
3. A malformed later cursor row preserves an earlier valid discovery and reports
   incomplete enumeration.
4. A repeated document ID is traversed/read once and reported.
5. Scan metadata round-trips through an isolated private cache.
6. Cancellation during a bounded slow provider query cannot replace prior cache.

Every scenario compares both fixture SHA-256 values and attempted mutations
before/after. Fixtures are two tiny synthetic CSV files owned by the **test APK**.
No corpus, seed or user-selected folder is read. The provider declares only read
capability, denies write opens/create/rename/delete, and counts mutation attempts.
The cache tests use unique temporary directories, never the application's real
scan cache. The provider and its manifest are entirely under `src/androidTest`;
they do not belong to the debug or release application manifest.

The harness requires API 29+ for shell-permission bootstrap. Instrumentation
briefly adopts `MANAGE_DOCUMENTS` to initialize the protected synthetic provider,
which grants read-only tree URI access to the target application; the shell
permission is dropped before scans. This bypasses a picker intentionally. The
permission-error case injects a provider exception; it does **not** validate the
system's grant/revocation UI, persisted grants or third-party provider behavior.
Cancellation is observed when the finite 500 ms provider query returns; this
harness does not promise interruption of an indefinitely blocking provider.

## Reproduction

Use a disposable emulator/device with API 29+, a full JDK and Android SDK.
The run installs debug application and test APKs. Set `ANDROID_SERIAL` when
multiple devices are attached:

```sh
export JAVA_HOME=/path/to/full-jdk
export ANDROID_HOME=/path/to/android-sdk
export ANDROID_SERIAL=emulator-5554
./scripts/verify_saf_provider.sh          # capability probe only
./scripts/verify_saf_provider.sh --run    # six synthetic device tests
```

The script copies current Android/core build inputs to an isolated snapshot,
runs only `SourceScannerProviderTest`, and emits a JSON receipt and Gradle log.
A run is accepted only with six executed tests, no skips/failures/errors, a
successful Gradle exit and unchanged recorded input bytes. No connected device
returns exit 2 and zero executed tests. Gradle compilation alone can be repeated
with `:androidApp:assembleDebugAndroidTest`; it is not device acceptance.

Test-only dependencies are `androidx.test:runner:1.7.0` and
`androidx.test.ext:junit:1.3.0`, verified against the official
[AndroidX Test release page](https://developer.android.com/jetpack/androidx/releases/test).
The six runtime tests remain pending until an Android runtime is available.
Recording selection and picker UI are separate acceptance work.
