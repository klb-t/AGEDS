# Android — odbiór nocnego przyrostu 2026-10-01

**BUILD SUCCESSFUL:** debug APK, 16 testów Android JVM i 10 testów desktop JVM.
Wszystkie 26 testów przeszły, bez błędów, porażek lub pominięć. To odbiór
kompilacji i testów hostowych; interakcje SAF na urządzeniu oraz realny ASR
pozostają oddzielnymi testami.

Build wykonano 2026-10-01 00:47–00:49 w strefie Europe/Amsterdam
(2026-09-30 22:47–22:49 UTC), lokalnie i bez GitHub Actions. Przebieg trwał
1 min 22 s. Wszystkie **57 zadań wykonano rzeczywiście**, przy wyłączonym
build cache, w nowym katalogu źródeł i wyników.

## Zakres

| Zestaw | Testy | Wynik |
|---|---:|---|
| Android: `SourceWorkbookParserTest` | 9 | passed |
| Android: `BoundedMetadataCacheTest` | 7 | passed |
| Desktop: `SourceTextParserTest` | 9 | passed |
| Desktop: `PriorityTest` | 1 | passed |

Sprawdzono m.in. parsowanie komórek i surowych lokatorów XLSX, odmowę
niedozwolonego XML/enkodowania, ograniczenia retencji i diagnostyki, parser
CSV/TSV oraz atomowy, ograniczony cache. Testy JVM nie odwzorowują zachowania
każdego dostawcy dokumentów Androida. API skanera i ekrany aplikacji zostały
skompilowane razem, ale nie wykonano interaktywnego testu telefonu.

## Powtórzenie

Wymagane: pełny JDK, zainstalowany Android SDK, Python 3, Bash i Git.
Wersje narzędzi i instrukcja bootstrapu: [ANDROID_BUILD.md](ANDROID_BUILD.md).
Uruchom w repo, wskazując własne katalogi narzędzi i pusty katalog wyników:

```sh
export JAVA_HOME=/path/to/full-jdk
export ANDROID_HOME=/path/to/android-sdk
./scripts/build_verify.sh /path/to/empty-result-directory
```

Skrypt uruchamia:

```sh
./gradlew --no-daemon --no-build-cache \
  :androidApp:assembleDebug :androidApp:testDebugUnitTest :core:desktopTest \
  --stacktrace
```

Kopiuje aktualne wejścia Android/core/Gradle do izolowanego katalogu tymczasowego.
Uwzględnia nowe, jeszcze niecommitowane pliki, o ile Git ich nie ignoruje.
Porównuje SHA-256 przed kopiowaniem i po buildzie, wykrywa także dodanie lub
usunięcie wejścia. Zmiana źródeł w trakcie powoduje odmowę końcowego odbioru.
Zachowuje log Gradle, XML testów, APK, wynik `apksigner` i `receipt.json`.
Nie nadpisuje istniejącego katalogu wyników. Proxy odczytuje z bieżącego
środowiska; walidacja TLS pozostaje włączona. Opcjonalne
`AGEDS_JAVA_TRUST_STORE` wskazuje właściwy trust store środowiska.

## Narzędzia i integralność

Odzyskano zachowany poza repo toolchain wcześniejszego, sprawdzonego builda,
co pozwoliło uniknąć ponownego pobierania. Ponownie sprawdzono rzeczywiste
`java`, `javac`, wrapper Gradle i metadane SDK: Temurin **21.0.12.1+1**,
Gradle **9.7.0**, platforma Android **37.0**, domyślne Build Tools **36.0.0**.
Wersji produkcyjnych zależności nie zmieniano. Dodano wyłącznie zależność
testową JUnit **4.13.2**, której rozwiązywanie także sprawdzono rzeczywiście.
Pochodzenie i wcześniejsza weryfikacja sum pobranych archiwów są opisane
w `ANDROID_BUILD.md`; ten przebieg nie pobierał ich ponownie.

Pełny [receipt JSON](ANDROID_NIGHT_BUILD_RECEIPT.json) zapisuje **35 hashy wejść**,
wyniki testów, czas, wersję JDK i hashe logów. `source_unchanged=true` potwierdza
zgodność końcowego checkoutu z budowanym snapshotem. Odczytany commit sam nie
identyfikuje ewentualnych niecommitowanych plików; wiążący jest wykaz hashy.

- APK: `AGEDS_night_debug.apk`, **14 025 749 bajtów**.
- SHA-256: `f20b9e04e127a89282064fcf992611e540b21921b097ae4dfc872de374fa89d1`.
- `apksigner verify --verbose --print-certs`: exit 0, podpis debug **v2**.

Pierwszy, wstępny build też przeszedł kompilację i 24 ówczesne testy, ale
w trakcie agenci ukończyli dodatkowe poprawki i dwa testy. Skrypt poprawnie
odmówił uznania tego snapshotu za końcowy (`source_unchanged=false`). Wynik
powyżej pochodzi z drugiego, świeżego snapshotu po tych poprawkach.

Pozostały ostrzeżenia o niewłączonych Android host tests **w module core**
(jego common tests wykonano jako desktop JVM) oraz o pakowaniu
`libandroidx.graphics.path.so` bez stripowania. Testy hostowe modułu
`androidApp` wykonały się w zadaniu `testDebugUnitTest`.
