# Dokładne archiwum metadanych

Format `ageds.inert-metadata-archive/v1` przechowuje zweryfikowany pakiet
`ageds.metadata-package/v1` w **nowym, odizolowanym pliku SQLite**. Nie jest bazą
roboczą AGEDS. Nie zawiera tabel roboczych `jobs`, `artifacts` czy `cases`:
zawiera wyłącznie `archive_envelope` i `archive_records` oraz znacznik formatu.
Nie podłączaj tego pliku jako `EW_DB_PATH`.

## Przepływ

```bash
python -m server.app.cli metadata-export snapshot.json
python -m server.app.cli metadata-archive-import snapshot.json history.sqlite
python -m server.app.cli metadata-archive-export history.sqlite roundtrip.json
python -m server.app.cli metadata-verify roundtrip.json
```

Każdy wynik wymaga **nowej nazwy pliku**. Istniejący wynik ani symlink nie zostaną
nadpisane. Import i eksport archiwum nie importują konfiguracji serwera, nie
otwierają live DB, nie uruchamiają migracji ani workerów. Historyczne lease,
statusy `running` i błędy pozostają dosłownymi metadanymi.

## Co znaczy „dokładny”

Odtworzone JSON ma identyczną reprezentację kanoniczną jak wejściowy pakiet:
`sorted-keys-compact-utf8-no-nan/v1`. Zachowuje ID, kolejność wierszy, relacje,
wszystkie wersje, cytaty segmentowe/słowne, błędy, surowe wartości pól, oryginalny
`exported_at`, flagi, istniejące digests i dodatkowe pola envelope/wierszy.
Nie poprawia niepoprawnego JSON zapisanego **wewnątrz pola tekstowego**,
np. historycznego `metadata_json`. Takie pole pozostaje tekstem. Dotyczy to również historycznych błędów
nieużywanych pól słów w `segments_json`: cytat segmentowy może pozostać poprawny,
gdy cytat słowny jest niedostępny. Weryfikacja cytatu odrzuca powtórzone klucze
selektora/segmentów i waliduje wszystkie faktycznie wybrane pola.

Utracone są wyłącznie szczegóły serializacji zewnętrznego JSON: wcięcia, kolejność
kluczy i równoważny zapis escape/liczb. To dokładność semantyczna/kanoniczna,
nie kopia oryginalnego pliku JSON bajt po bajcie. W raporcie `package_sha256`
obejmuje cały kanoniczny pakiet wraz z `integrity`; `payload_sha256` ma istniejące
znaczenie pakietu (bez jego sekcji `integrity`). Import sam sprawdza pełny
roundtrip przed atomową publikacją archiwum.

## Odczyt niezaufanych danych

- JSON: UTF-8, bez powtórzonych kluczy, NaN/Infinity, nieskończoności wskutek
  przepełnienia wykładnika ani niepoprawnych surrogate strings.
- Cały graf i przypięte cytaty są sprawdzane przed utworzeniem wynikowego pliku.
  Brakujące tabele starego schematu pozostają jawnie oznaczoną nieznaną historią.
- Ścieżki/URI zapisane w rekordach są dosłownymi wartościami. Nie są otwierane,
  kopiowane, normalizowane ani pobierane z sieci.
- Jawnie wskazany plik wejściowy musi być regularnym plikiem; końcowy symlink
  i FIFO są odrzucane. Wymagane jest POSIX `O_NOFOLLOW`. Nie jest to sandbox
  drzewa rodziców wskazanej ścieżki.
- SQLite jest odczytywany do ograniczonego bufora i otwierany w pamięci, aby nie
  utworzyć sidecarów przy źródle. Odczyt wymaga dokładnego schematu; dodatkowe
  tabele, widoki, triggery i indeksy są odrzucane. Wyłączone trusted schema,
  query-only, kontrola struktury SQLite i budżet instrukcji ograniczają odczyt.
- Wynik jest zapisany w tymczasowym pliku, zsynchronizowany i publikowany przez
  wyłączny hardlink. Istniejący plik i konkurencyjny zapis wygrywający wyścig są
  zachowane. Potrzebny jest filesystem wspierający hardlink w katalogu wyniku.

Domyślne limity: JSON 32 MiB, SQLite 128 MiB, 100 000 wierszy, 2 000 000 węzłów
JSON, głębokość 64. CLI udostępnia `--max-input-bytes` (limit pakietu JSON w obu
kierunkach), `--max-archive-bytes` i `--max-rows`. Python API dodatkowo udostępnia
`ArchiveLimits(max_nodes=..., max_depth=...)`. Limity są dodatnimi liczbami
całkowitymi. Przekroczenie kończy operację; nie powstaje częściowe archiwum.

Kody CLI: `0` — zapis/odbiór zakończony, `1` — `metadata-verify` wykrył niezgodny
pakiet, `2` — błąd odczytu, struktury wejścia, limitu lub publikacji. Błędy mają
postać JSON na stderr. Informacje o granicach odbioru są też w raporcie wyniku.

## Granica kontraktu

To archiwum metadanych, **bez źródłowych mediów, podpisu, przywrócenia live DB,
przenoszenia tożsamości do nowej sprawy, wznowienia zadań ani replay inferencji**.
Hash wykrywa zmianę względem znanej wartości; nie potwierdza autorstwa ani
prawdziwości. Celowa zamiana całego pakietu wraz z przeliczonymi hashami wymaga
zewnętrznego zaufanego digestu lub przyszłego podpisu, aby zostać wykryta.

## Odbiór

`server/tests/test_metadata_archive.py` sprawdza dokładną kanoniczną równość,
wersje i błędne pola historyczne, przypięcia cytatów, odrzucenie niepoprawnego
grafu, schematu SQLite, ingerencji w wiersze, duplikatów JSON, wyścigu publikacji,
limitów i niepożądanych zapisów. `server/tests/test_cli.py` sprawdza kompletny
przepływ obu poleceń i błędy bez uruchamiania konfiguracji live.
