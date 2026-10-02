# Current validation

Recovery acceptance on **2026-10-02**. Each receipt identifies the source and
actual environment it tested. It is not a claim of deployed service or device
execution.

| Scope | Results |
|---|---|
| Backend and real CLI subprocesses | [Report](BACKEND_COMPLETION_2026-10-02.md), [source/dependency receipt](BACKEND_COMPLETION_2026-10-02.json) |
| Browser and Node | [Initial report](BROWSER_COMPLETION_2026-10-02.md), [final template acceptance](BROWSER_PRIVACY_2026-10-02.md), [final receipt](BROWSER_PRIVACY_2026-10-02.json) |
| Native Kotlin/Android | [Report](NATIVE_COMPLETION_2026-10-02.md), [receipt](NATIVE_COMPLETION_2026-10-02.json) |
| Clean ASR installation and real synthetic inference | [Report](ASR_COMPLETION_2026-10-02.md), [installation/inference receipt](ASR_COMPLETION_2026-10-02.json) |

Native completion: 411 executed JVM tests, main APK signature verification and
synthetic-provider test APK compilation passed with pinned versions. SAF device
execution remains an independent acceptance gate.

Earlier evidence, initial failures and experiments are preserved in the
[historical archive](../archive/2026-09-30_2026-10-01/README.md). Use
[the handoff](../../HANDOFF.md) for current limitations and
[development instructions](../DEVELOPMENT.md) to rerun checks.
