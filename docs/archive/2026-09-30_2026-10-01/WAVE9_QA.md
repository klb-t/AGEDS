# Fala 9 — wykonanie natywnej polityki skanowania

Wspólny SourceScanEngine wykonuje tę samą politykę w cienkim adapterze SAF
oraz na syntetycznym dostawcy hosta. Dostawca oferuje wyłącznie leniwy kursor,
URI i strumień odczytu. Nie dodano zapisu źródeł ani nowej biblioteki.

50 nowych testów: 6 właściciela, 14 niezależnej polityki, 20 cyklu zasobów,
5 mieszanych formatów i 5 przepływu do cache. Wszystkie weszły do rzeczywistego
Gradle: 337 testów JVM przeszło. CSV/TSV UTF16, XLSX, XLS, WAV oraz inventory
korzystają z produkcyjnych parserów; raw wartości, formuły bez ewaluacji,
kolizje URI, częściowe pokrycie i pełny/prefiksowy hash pozostają rozdzielone.

Przegląd wykrył odczyt dodatkowej porcji metadanych po limicie wpisów.
Sprawdzenie następuje teraz przed next(); dokładnie pełny katalog ma konserwatywne
entry_limit bez spekulatywnego odczytu EOF. Istniejący bajt sentinel dla nie-WAV
pozostaje policzony i jawny; WAV ma ścisły limit bajtów.

Odbiór szczegółowy: SOURCE_SCAN_ENGINE.md, ANDROID_SOURCE_SCAN_ADAPTER.md,
ANDROID_SOURCE_ENGINE_MIXED_FORMATS.md, WAVE9_SCAN_POLICY_QA.md,
WAVE9_LIFECYCLE_QA.md, WAVE9_CACHE_FLOW_QA.md i ANDROID_WAVE9_BUILD.md.
Pierwszy nieudany build i poprawka classpath testu są zachowane w receiptach.

Nie wykonano telefonu/SAF runtime. Cache roundtrip zachowuje typowane metadane,
nie leksykalne bajty JSON. Mniejszy skonfigurowany limit cache przetestowano na
rzeczywistym wyniku skanu; nie deklarujemy próby >4 MiB w tej fali.

Następny konkretny zakres: wspólny budżet zdekodowanego JSON i pracy projekcji
cytatów w inertnym archiwum; wykryto obejście limitu outer-only i powtarzane
dekodowanie tej samej wersji. Oddzielna kolejka: zlinearyzować anulowanie skanu
z publikacją cache (okno między ensureActive a Files.move).
