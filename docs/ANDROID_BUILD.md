# Android: lokalny build i stan weryfikacji

Sprawdzenie: 2026-09-30. Budowanie i testy uruchamiano lokalnie, bez GitHub Actions.

## Polecenie

Potrzebny jest **pełny JDK z `javac`**, nie samo JRE, oraz Android SDK.
`JAVA_HOME` wskazuje JDK, a `ANDROID_HOME` katalog SDK.

```sh
sdkmanager --sdk_root="$ANDROID_HOME" 'platforms;android-37.0' 'build-tools;36.0.0' 'platform-tools'
./gradlew :androidApp:assembleDebug :core:desktopTest --stacktrace
```

SDK jest narzędziem builda; jego instalacja nie wymaga dostępu do prywatnego korpusu.
APK powstaje w `androidApp/build/outputs/apk/debug/androidApp-debug.apk`.
Wyniki testów JVM: `core/build/test-results/desktopTest/`.

## Wersje i oficjalne źródła

| Składnik | Wersja użyta | Sprawdzenie |
|---|---|---|
| Gradle | 9.7.0 | Oficjalny ZIP pobrany i SHA-256 porównany z oficjalną sumą. |
| Kotlin/KGP i compiler plugins | 2.4.20 | Marker POM i plugin POM dostępne w Maven Central; rozwiązywane przez rzeczywisty build. |
| Android Gradle Plugin | 9.4.0 | Oficjalne Maven metadata i rzeczywista konfiguracja modułów. |
| Compose Multiplatform plugin | 1.12.1 | Oficjalne Maven metadata i plugin resolution. |
| Android SDK | API 37.0 | Nazwa pakietu w obecnym katalogu: `platforms;android-37.0`, nie `platforms;android-37`. Repo zachowuje `compileSdk = 37`. |
| Android Build Tools | 36.0.0 | Wersja domyślna AGP 9.4; automatyczna instalacja przez rzeczywisty build. Dodatkowo sprawdzono dostępność 37.0.0. |
| JDK | Eclipse Temurin 21.0.12.1+1 | Pełny JDK pobrany z oficjalnego repozytorium Adoptium, SHA-256 zgodny z sumą publikacji. |
| Android command-line tools | 15859902 | Oficjalny Linux ZIP, SHA-256 zgodny z Android Developers. |

Podczas bootstrapu nie zmieniono wersji zależności w projekcie. Wersje istniały w oficjalnych repozytoriach; początkowe komunikaty o braku pluginu wynikały z konfiguracji sieci lokalnego wykonania.

Źródła:

- [Gradle 9.7.0 ZIP](https://services.gradle.org/distributions/gradle-9.7.0-bin.zip), [SHA-256](https://services.gradle.org/distributions/gradle-9.7.0-bin.zip.sha256).
- [AGP 9.4: kompatybilność](https://developer.android.com/build/releases/agp-9-4-0-release-notes): JDK 17+, Gradle 9.6+, API do 37 i domyślne Build Tools 36.0.0.
- [KGP: tabela kompatybilności](https://kotlinlang.org/docs/gradle-configure-project.html): Kotlin 2.4.20 obejmuje Gradle do 9.7.0; ostatni w pełni przetestowany AGP w tabeli to 9.3.1. Wynik lokalny dla 9.4.0 jest osobnym sprawdzeniem, nie rozszerzeniem gwarancji producenta.
- [Android command-line tools](https://developer.android.com/studio).
- [Eclipse Temurin 21](https://github.com/adoptium/temurin21-binaries/releases/tag/jdk-21.0.12.1+1).
- [Kotlin Maven metadata](https://repo.maven.apache.org/maven2/org/jetbrains/kotlin/kotlin-gradle-plugin/maven-metadata.xml), [AGP Maven metadata](https://dl.google.com/dl/android/maven2/com/android/tools/build/gradle/maven-metadata.xml).

## Rozwiązane problemy bootstrapu

1. Repo zawierało `gradle-wrapper.properties`, ale brakowało launcherów i wrapper JAR. Dodano oficjalny wrapper z Gradle 9.7.0. Generowano go offline z już pobranego i sprawdzonego ZIP, ponieważ walidacja HEAD w zadaniu generacji przekraczała swój limit 10 s. Końcowy wrapper wskazuje oficjalny HTTPS URL, ma `distributionSha256Sum`, `networkTimeout=60000` i `validateDistributionUrl=true`.
2. Systemowy Java 17 był JRE bez kompilatora. Rzeczywisty Gradle zatrzymał się z `does not provide the required capabilities: [JAVA_COMPILER]`. Zainstalowano pełny JDK 21 zgodny z konfiguracją CI, w odrębnym katalogu roboczym.
3. Proxy wykonawcze może zmieniać port między wywołaniami środowiska. Trwałe zapisanie takiego portu powodowało `Connection refused`, a Gradle przedstawiał ten problem jako niemożność rozwiązania pluginu. Ustawienia proxy trzeba odczytać z bieżącego środowiska i zastosować w tym samym procesie startowym co build. Adresów i portów tego środowiska nie zapisano w repo.
4. Przy użyciu dostarczonego JDK skonfigurowano hostowy Java CA store, aby zachować walidację TLS platformy. Nie wyłączano weryfikacji certyfikatów.

## Wynik

**BUILD SUCCESSFUL** dla `./gradlew :androidApp:assembleDebug :core:desktopTest` na końcowym snapshotcie źródeł. Finałowy build bez poprzednich katalogów output: 36 s, 50 zadań (24 wykonane, 26 z poprawnie dobranego cache Gradle). SHA-256 wszystkich źródeł Android/core, plików konfiguracyjnych i wrappera porównano z bieżącym repo po zakończeniu: brak różnic.

- Test JVM `PriorityTest[desktop]`: 1 test, 0 failures, 0 errors, 0 skipped. Dotyczy reguły priorytetu; nie stanowi testu UI ani skanowania SAF.
- APK: `AGEDS_foundation_debug_2026-09-30.apk`, 14 091 343 bajty.
- SHA-256 APK: `b7d4548a12fa23ea20b76f30d13e742dc4c2c19562985418b730e9f48a57bc91`.
- `apksigner verify --verbose --print-certs`: exit 0, poprawny podpis debug w schemacie v2.
- Podczas pierwszego pełnego builda test JVM wykonał się rzeczywiście; weryfikacja końcowego snapshotu mogła wykorzystać ten wynik z cache, ponieważ wejścia testu nie zmieniły się.

Jedna próba incremental packaging w synchronizowanym katalogu roboczym trafiła na znikający techniczny plik `.rsync-tmp/classes.dex`. Końcowy build wykonano w odizolowanym snapshotcie źródeł i narzędzi. Rozwiązanie nie zmieniło kodu ani wersji i nie pomijało zadań kompilacji lub testów.

Pozostają ostrzeżenia o przestarzałych formach DSL `androidLibrary` i `js(IR)`, opt-in Wasm oraz niewłączonych Android host tests. Testy Desktop działały. Bibliotekę `libandroidx.graphics.path.so` spakowano bez stripowania; nie zablokowało to builda.

Build nie jest testem interakcji na telefonie ani wykonania ASR. Oryginały korpusu nie są używane w testach kompilacji.
