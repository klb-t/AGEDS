# Android — fala 2, 2026-10-01

**BUILD SUCCESSFUL: 74 testy JVM, zero błędów i pominięć.**
Końcowy izolowany snapshot wykonał wszystkie 57 zadań Gradle bez build cache,
w 2 min 10 s. Przebieg: 2026-09-30 23:21:00–23:23:11 UTC
(2026-10-01 01:21–01:23 Europe/Amsterdam).

| Zestaw | Testy |
|---|---:|
| Android — XLS, własne | 14 |
| Android — XLS, niezależne adwersarialne | 12 |
| Android — formaty, niezależne adwersarialne | 8 |
| Android — CSV/kodowania | 10 |
| Android — kodowania XLSX | 4 |
| Android — dotychczasowy XLSX | 9 |
| Android — cache metadanych | 7 |
| Desktop — parser tekstu i priorytet | 10 |
| **Razem** | **74** |

`docs/ANDROID_WAVE2_BUILD_RECEIPT.json` zawiera **45 hashy wejść**, wszystkie
zestawy testów, czas, wersję JDK oraz hashe APK/logów. Źródła snapshotu i checkoutu
pozostały zgodne (`source_unchanged=true`). Odczytany commit poprzedza commitowanie
nowych plików; dokładną tożsamość budowanego kodu ustala lista hashy.

- APK: 14 058 517 bajtów, podpis debug v2 sprawdzony `apksigner`, exit 0.
- SHA-256: `a4fce4b144cb13e3a7637421f6d5bebb4d638ed94221f0360b030db27b026605`.
- JDK Temurin 21.0.12.1+1, Gradle 9.7.0, platforma Android 37.0.

Reprodukcja: `scripts/build_verify.sh /path/to/empty-results` z pełnym
`JAVA_HOME` i `ANDROID_HOME`. Ten przebieg ograniczył współbieżność przez
`GRADLE_OPTS='-Dorg.gradle.workers.max=1 -Dorg.gradle.jvmargs=-Xmx1024m'`.
SDK/cache odzyskano z poprzedniego builda; nie dodawano płatnych usług ani CI.
Równoległe próby wstępne napotkały blokady cache oraz zniknięcie demona Gradle;
nie uznano ich za końcowy odbiór. Przyczyna zniknięcia demona nie została
jednoznacznie ustalona. Końcowy build był sekwencyjny.

Niezależne 34 bezpośrednie testy Kotlin/JUnit są dodatkowym sposobem wykonania
części tych samych testów, **nie kolejnymi 34 odrębnymi przypadkami**.
Sześć testów SAF ma osobny dowód kompilacji aplikacji testowej i **zero wykonań
na Androidzie**: `docs/ANDROID_SAF_PROVIDER_ACCEPTANCE.md`. Ten build nie oznacza
odbioru telefonu, uprawnień SAF, pickera ani jakości ASR.

Pozostały wcześniejsze ostrzeżenia o braku Android host tests modułu core
(jego testy wykonano na desktop JVM) oraz o niepoddanej stripowaniu bibliotece
`libandroidx.graphics.path.so`. Receipty fali 1 pozostają zachowane.
