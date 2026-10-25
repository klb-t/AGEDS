# Rzecznik materiału dowodowego — projekt modelu rozszerzenia AGEDS (2026-10-25)

Status dokumentu: **projekt, bez implementacji**. Wymaganie: streszczenie rozmowy właściciela z GPT
(`chatadhd/docs/architecture/AGEDS_RZECZNIK_GPT_2026-10-25.md`, wierne skrócenie). Wymaganie traktuję w całości;
jego przykłady są przykładami (silnik ma obsłużyć każdą konfigurację, także mało użyteczną). Konkretne wartości
poniżej to **presety w danych** albo **przykłady**, nigdy wymagania. Nic tu nie rozstrzyga, który dostawca
modelu dostaje dane właściciela (to jego decyzja tak/nie, pkt 7).

Etykiety: **[JEST]** istnieje i działa (z lokatorem w kodzie), **[PROPONOWANE]** projekt tego dokumentu,
**[PLANOWANE]** pakiety z pkt 6, jeszcze nie rozpoczęte. Żadna pozycja PROPONOWANE/PLANOWANE nie może być
opisywana użytkownikowi jako działająca (AGENTS.md: „Nie opisuj planowanej zdolności jako dostępnej”).

## 1. Inwentarz: co AGEDS już ma [JEST]

Stan z `main` (HEAD przy pisaniu), serwer FastAPI + SQLite (`server/app/`).

| Obszar | Co jest | Lokator |
|---|---|---|
| Sprawa | tabela `cases(id,name,created_at)`; **każde** `sources.case_id` jest `NOT NULL` | `server/app/db.py:12-16`, `:18-26` |
| Źródło (akwizycja) | `sources(kind,label,locator,account,metadata_json)`; `ensure_source(..., case_id=)` | `db.py:18`, `evidence.py:63-77` |
| Materiał / artefakt | `artifacts` z `sha256`, `parent_artifact_id`, `source_id`, `is_raw`, `stored_path`; `UNIQUE(sha256, source_locator)` | `db.py:29-47` |
| Pozyskania (wersje akwizycji) | `source_observations`, append-only (triggery) — obserwacja bajtów, nie prawda | `db.py:198-231` |
| Artefakt pochodny / wersja tekstu | `derived_text(kind,model,language,text,segments_json,run_id)`; każdy run to nowy wiersz | `db.py:70-80`, `evidence.py:166-188` |
| Proweniencja przetwarzania | `processing_runs` (tool, model, model_version, parameters, lease) | `db.py:235-252` |
| Cytat | `evidence_anchors` (wersja `derived_text_id`, zakres ms, `quote_sha256`, `selector_json`), append-only; selektor słowny wymaga znaczników ASR | `db.py:255-275`, `server/app/citations.py:133-209`, `AGENTS.md` |
| Adnotacje | `annotations(artifact_id|event_id|derived_text_id, kind, body)` — **bez autora i bez zakresu widoczności** | `db.py:82-94` |
| Graf | tabela `links(left_kind,left_id,right_kind,right_id,relation,confidence,rationale)` — **bez case_id, bez autora, bez statusu** | `db.py:111-121` |
| Tagi | `tags` (globalna nazwa UNIQUE), `artifact_tags`, `event_tags` | `db.py:96-109` |
| Audyt | `audit_log(actor DEFAULT 'system', ...)`, append-only | `db.py:137-146`, `:236-243` |
| Wyszukiwanie | jeden **globalny** indeks FTS5 `search_fts(object_kind,object_id,title,content)`; `search_page()` bez filtra zakresu | `db.py:148-153`, `server/app/search.py:106-141`, `main.py:173-185` |
| Paczki metadanych | `export_metadata_package()` zrzuca **wszystkie** tabele (`TABLES`), `metadata_only`, `signed:false`, `replay_supported:false`; walidacja i archiwum | `server/app/packages.py:17-34,54-86`, `archive.py` |
| Paczka cytatu | `export_citation_packet`, niezależny konsument | `server/app/exchange.py:104`, `exchange_consumer.py`, `docs/EVIDENCE_CONSUMER.md` |
| Kolejka/worker | lease + fencing, publikacja atomowa | `jobs.py`, `worker.py`, `AGENTS.md` |
| Klient Android | Evidence Workbench; korpus, skaner SAF, cytaty, lokalne cache metadanych; **dalej bez kont** | `androidApp/src/main/java/dev/klbt/ageds/*`, `core/src/commonMain/.../CorpusModels.kt` |
| HTTP API | ~30 tras (`/api/artifacts`, `/api/search`, `/export/*`, `/api/packages/validate` ...) | `server/app/main.py:96-467` |

**Czego nie ma (luki, które projekt musi zamknąć) [JEST = brak]:**

1. **Brak kont, ról, uprawnień i uwierzytelniania.** `main.py` nie ma żadnego `Depends`/nagłówka auth;
   jedyny `actor` to pole tekstowe w `audit_log` (`db.py:139`). Każdy, kto dosięgnie portu, widzi wszystko.
2. **Materiał należy do dokładnie jednej sprawy** (przez `sources.case_id NOT NULL`), a `UNIQUE(sha256,source_locator)`
   nie daje „jeden materiał w wielu sprawach”. Wymaganie „materiał istnieje raz, relacja wiele-do-wielu” wymaga
   osobnej relacji; dziś dwa wejścia tego samego pliku z różnych lokatorów to dwa artefakty.
3. **Brak pojęcia fragmentu poza cytatem** (cytat to kotwica na konkretnej wersji tekstu; nie ma fragmentu
   „strony 3–5 PDF-a” ani „wiadomości 17–40 wątku” jako adresowalnego obiektu).
4. **Pomysł „pakietu” istnieje tylko jako eksport metadanych całej bazy** (`packages.py`), nie jako zakres
   udostępnienia.
5. **Powierzchnie wycieku w dzisiejszym kodzie** (każda musi przejść przez autoryzację, gdy powstaną konta):
   globalny FTS (`search.py:128`), `links` (graf bez zakresu), `annotations`, globalne `tags`, `audit_log`
   (`details_json` zawiera np. nazwę artefaktu), `/export/events.csv` i `/export/manifest.json` (`main.py:196-207`),
   `export_metadata_package` (cała baza), `api_artifact_content` (bajty, `main.py:367`), cache klienta Android
   (`BoundedMetadataCache.kt`, `SourceScanCache.kt`).
6. **Brak rzecznika**: żadnego kodu wywołującego model do rozmowy; istniejące „LLM” to tylko run ASR.

Zasady repozytorium, które zachowuję: skan tylko do odczytu; metadane poza drzewem źródłowym; oddzielenie
treści/pozyskania/obserwacji parsera/ASR/interpretacji; konflikt zachowany z lokatorami obu stron; run per próba
modelu (brak = `unknown`); migracje addytywne, bez przebudowy tabel i bez przepisywania historii
(`db.py:migrate_db`); testy na trudnych danych syntetycznych (brak prywatnych materiałów).

## 2. Rozszerzenie modelu [PROPONOWANE]

### 2.1 Zasada: rozszerzamy, nie budujemy równolegle

Nie tworzymy drugiej bazy ani drugiej ontologii. Istniejące `cases`, `sources`, `artifacts`, `derived_text`,
`evidence_anchors`, `links`, `annotations`, `audit_log` zostają. Dodajemy **addytywne tabele** (ten sam
mechanizm `ADDITIVE_SCHEMA` + `schema_migrations`, `db.py:189-293`), a rzeczy dziś bez właściciela/zakresu
dostają **kolumny addytywne** (`_add_column`, `db.py:277-283`) z wartością domyślną „niejawne dziedziczenie”
opisaną w 2.9. Nic nie jest przepisywane; wiersze historyczne mają `NULL` = „sprzed modelu uprawnień” i
zachowują się według jawnego ustawienia (2.9, `legacy_scope_policy`).

### 2.2 Byty i tożsamość

Wszystkie nowe byty mają **stabilny identyfikator niezależny od nazwy i ścieżki** (kolumna `uid TEXT UNIQUE`,
losowy, plus dotychczasowe `id INTEGER` jako klucz wewnętrzny; wartość uid to preset generatora, zapisana
razem ze schematem, wymienialna). Metadane przypisań odwołują się przez uid — to realizuje „stabilne
identyfikatory, nie nazwa/ścieżka” i pozwala metadanym leżeć w osobnych plikach/archiwach
(`packages.py` eksportuje już tabelami; pakiet zakresowy z 2.6 robi to samo dla wycinka).

| Byt | Realizacja | Uwagi |
|---|---|---|
| **Użytkownik** (`principals`) | nowa tabela `principals(uid, display_name, kind, status, metadata_json, created_at)`; `kind` to otwarty tekst (człowiek, usługa, agent, ...) | Role **nie** są typem konta (wymaganie): rola = zbiór uprawnień z relacji. Uwierzytelnianie jest odrębną warstwą (2.7); brak uwierzytelnienia = tryb dzisiejszy, jawnie nazwany `legacy_open` |
| **Materiał źródłowy** | istniejące `artifacts` (+ `uid`), `source_observations`, `derived_text` | patrz 2.3 |
| **Sprawa** | istniejące `cases` (+ `uid`, `metadata_json`, `owner_principal_id` nullable) | sprawy właściciela są ze sobą powiązane przez `case_relations` (2.4), nie przez hierarchię |
| **Przypisanie** | nowa `assignments` (2.4) | relacja z własnymi metadanymi |
| **Pakiet** | nowa `packages` + `package_items` (2.6) | zakres, nie kopia |
| **Uprawnienie** | nowa `grants` (2.5) | relacja principal -> zakres |
| **Fragment** | nowa `fragments` (2.3) | adresowalny zakres w wersji materiału |

### 2.3 Materiał: źródło / wersja / fragment / artefakt pochodny

Cztery rozróżnienia z wymagania, zmapowane na istniejące mechanizmy:

- **Źródło** = `artifacts` + `sources`. Oryginał niezależny od przypisań: nic z 2.4 nie modyfikuje
  `artifacts` (poza dodaniem `uid`).
- **Wersja** = (a) wersja bajtów: nowy `artifacts` o innym `sha256` połączony relacją `supersedes` w
  `artifact_versions(artifact_id, prev_artifact_id, relation, observed_at)` [PROPONOWANE]; (b) wersja
  tekstu: istniejące `derived_text` (każdy run to wiersz). Odpowiedź rzecznika zapisuje `basis` (2.8) na
  konkretnych `artifact_id`+`sha256`+`derived_text_id`, więc aktualizacja nie zmienia ustalenia, na czym
  oparto wcześniejszą odpowiedź.
- **Fragment** = `fragments(uid, artifact_id, derived_text_id NULL, selector_json, kind, created_by, created_at)`.
  `selector_json` jest otwartą strukturą (zakres ms, zakres znaków, strona, zakres zdarzeń w wątku, ...);
  istniejący `evidence_anchors` jest **jednym rodzajem fragmentu** (`kind='citation'`) i zostaje źródłem
  prawdy dla cytatów; `fragments` go referencjonuje (`anchor_id`), nie duplikuje. Fragment jest też
  celem przypisania.
- **Artefakt pochodny** = istniejący `derived_text.kind` (otwarty tekst: transkrypcja, OCR, tłumaczenie,
  streszczenie, analiza ...). Brakuje: (i) **opisu przekształcenia i odniesienia do źródła** — daje to
  `processing_runs` dla ASR; dla pochodnych tworzonych przez rzecznika dodajemy `derivations(derived_text_id,
  input_refs_json, transformation_json, run_id)` [PROPONOWANE]; (ii) **śladu zależności od materiałów**:
  `input_refs_json` to lista uid materiałów/fragmentów/wersji, z której wynika `derived_scope` (2.8).
- **Referencja do źródła zewnętrznego** jest pełnoprawna: `artifacts.stored_path` może być `NULL`, a
  `external_ref_json` (kolumna addytywna) opisuje URL/identyfikator + opcjonalne `sha256` + datę obserwacji.
  Bez wymuszania importu; brak bajtów = jawna informacja `bytes_available=false`, nie błąd.
- **Cache, indeks, snapshot, eksport** to osobne obiekty z własnym wierszem `derived_state` (2.8), nigdy
  część materiału.

### 2.4 Przypisanie jako relacja z własnymi metadanymi

```
assignments(
  uid, case_id, target_kind, target_id,      -- cel: artifact | fragment | derived_text | event | ...
  issue_ref_json,                            -- zagadnienie/twierdzenie (otwarta struktura lub odniesienie do wiersza)
  meaning_text,                              -- znaczenie w TEJ sprawie
  rationale_text, status, status_changed_at,
  author_principal_id, author_kind,          -- człowiek | AI | import | ...
  created_at, visibility_scope_id,           -- kto widzi TO przypisanie (2.5); domyślnie: ta sprawa
  model_run_id NULL,                         -- jeśli autorem jest AI: processing_runs.id
  metadata_json )
```

- `status` jest **otwartym tekstem** z presetami (np. `ai_proposed`, `human_reviewed`, `rejected`, ...) —
  przykłady z wymagania, nie zamknięta lista; zbiór wartości trzyma tabela ustawień (2.9), a nieznana
  wartość nie jest błędem (ostrzeżenie informacyjne, nie odrzucenie).
- Ten sam `(target)` może mieć **wiele przypisań w wielu sprawach i wiele w tej samej** (różni autorzy,
  różne zagadnienia); silnik nie sprawdza „sensowności” — najwyżej doradcze ostrzeżenie (filozofia: mało
  użyteczna konfiguracja nie jest błędem).
- **Brak globalnego znaczenia dowodowego**: nie ma kolumny „waga/znaczenie” na `artifacts`; znaczenie
  istnieje wyłącznie w `assignments.meaning_text` w kontekście `case_id`.
- Usunięcie przypisania = **wycofanie** (`status='withdrawn'`, `withdrawn_at`), nie `DELETE` (spójne z
  append-only audytem i anchors); nie narusza źródła ani przypisań innych spraw. Twarde usunięcie jest
  osobną, jawną operacją właściciela (z wpisem audytu), też bez skutku na źródło — klucze obce tylko
  od przypisania do celu, nigdy odwrotnie (brak `ON DELETE CASCADE` w stronę materiału).
- `case_relations(case_a, case_b, relation, metadata_json)` — „wszystkie sprawy właściciela są powiązane”
  jako dane, nie założenie w kodzie. Relacja **nie** daje dostępu (2.5).
- Istniejące `links` (graf) rozszerzamy addytywnie: `case_id NULL`, `author_principal_id NULL`,
  `visibility_scope_id NULL`, `status NULL`. Link bez zakresu = „sprzed modelu”.

### 2.5 Uprawnienia

```
scopes(uid, kind, spec_json)      -- zakres: 'case' | 'package' | 'set' | 'filter' ... (otwarty tekst)
grants(uid, principal_id, scope_id, capabilities_json, granted_by, granted_at,
       valid_from, valid_to, revoked_at, revoked_by, conditions_json, metadata_json)
```

- `capabilities_json` to **otwarty zbiór** zdolności (np. `read_metadata`, `read_text`, `read_bytes`,
  `cite`, `annotate`, `propose`, `invite`, `chat_with_advocate`, `export`). To przykłady; silnik traktuje
  napis jako nieprzezroczysty klucz, a zestaw zdolności „obsługiwanych przez kod” jest rejestrem danych
  (`capability_registry`), uzupełnialnym bez zmiany schematu. Nieznana zdolność w grancie = nic nie
  przyznaje (domyślna odmowa), ale jest zachowana.
- **Zdolności są niezależne**: prawo do odczytu tekstu nie daje bajtów oryginału, nie daje eksportu, nie daje
  zapraszania — to są właśnie nierozstrzygnięte pytania (2.9).
- Zakres jest **przecięciem**, nie sumą z „pokrewnych spraw”: dostęp do materiału X w sprawie A nie
  daje dostępu do sprawy B, do przypisań z B ani do prywatnych adnotacji właściciela (wymaganie).
- **Efektywny dostęp do widoku (read-view)** = jedna funkcja
  `visible(principal, object, capability, at_time)`, wyznaczana wyłącznie z `grants` + reguł
  widoczności obiektu; **jedno miejsce** (`authz.py`) i tylko ono wolno wywoływać dla treści trafiających do
  odpowiedzi. Brak implementacji równoległych w trasach.
- **Cofnięcie** (`revoked_at`) działa od chwili zapisu na wszystkie ścieżki systemu (wyszukiwanie, cytaty,
  cache, pamięć rozmowy, eksporty); nie cofa kopii już pobranych (pokazywane w UI jako ostrzeżenie przy
  udzielaniu prawa do pobierania).
- Domyślnie: brak grantu = brak dostępu (`default_deny`); właściciel (principal z `owner_principal_id`
  sprawy / flagą instalacji) ma domyślny preset „pełny zakres własnych spraw”, zapisany jako zwykły grant,
  nie jako obejście w kodzie.

### 2.6 Pakiet

`packages(uid, name, owner_principal_id, mode, created_at, metadata_json)`,
`package_items(package_id, item_kind, item_id, selector_json, include_json, added_at, withdrawn_at)`.

- Pakiet = **zestaw referencji i zakresów** (sprawy, materiały, fragmenty, przypisania, widoki metadanych,
  zdolności), nigdy kopia bajtów. Wspólny materiał w kilku sprawach pakietu ma **jeden uid**; w raportach
  „wystąpienia nie są niezależnymi dowodami” (pole `independence_group` wynika z uid źródła, nie z liczby
  wystąpień; dedupe w podsumowaniach).
- `mode` (ustawienie, pkt 2.9): `live` (wskaźniki do bieżącego stanu) albo `snapshot` (zamrożona lista
  wersji `artifact_id+sha256+derived_text_id` i przypisań z `created_at <= T`), albo mieszane per
  element. Oba tryby muszą działać; wybór nie jest przesądzony.
- Udostępnienie pakietu = utworzenie `scope(kind='package')` i `grants` na ten scope. Pakiet nie
  zmienia źródła; usunięcie z pakietu / cofnięcie grantu nie usuwa materiału.
- Istniejące `export_metadata_package` zostaje jako **eksport administracyjny całej bazy** (tylko
  właściciel, jawnie oznaczony). Dla odbiorcy powstaje **eksport zakresowy** (`export_scope_package(scope,
  principal)`) o tym samym kształcie kanonicznym i integralności, ale zbudowany wyłącznie z wierszy
  przepuszczonych przez `visible()`; zawiera `scope_digest` i listę pominiętych *kategorii* (bez ich
  zawartości ani liczności, jeśli liczność mogłaby ujawnić, patrz 2.9 `count_disclosure`).

### 2.7 Uwierzytelnianie i sesje [PROPONOWANE]

Autoryzacja jest przed treścią; tożsamość dostarcza warstwa wymienialna: `auth_methods` (token API per
principal, lokalnie wygenerowany; później OIDC itp. — otwarte). Domyślny preset to osobny token na principala
przechowywany jako hash (sól + SHA-256/KDF, parametry w danych). Tryb `legacy_open` (dzisiejszy, bez kont)
zostaje dostępny jako jawnie nazwane ustawienie dla pracy lokalnej właściciela, z bannerem; nie jest
domyślny, gdy aktywny jest dowolny `grant` dla kogoś innego niż właściciel (ostrzeżenie doradcze, nie
blokada — filozofia: nie narzucamy, ale mówimy).

### 2.8 Rzecznik: autoryzacja PRZED modelem

Rzecznik (`advocate/`) nigdy nie dotyka bazy bezpośrednio. Jedyne źródło treści dla modelu to
**`ContentGate`**, który zwraca wyłącznie obiekty już przefiltrowane `visible(principal, ...)`.

Przepływ pojedynczego pytania:

1. `session = (principal, conversation_id, settings_snapshot)`; `ctx = AuthzContext(principal, at=now)`.
2. **Wyszukiwanie**: zapytanie idzie do indeksu z filtrem zakresu **w samym zapytaniu** (kolumna
   `scope_key` w indeksie, patrz niżej), nie po stronie wyników; wyniki spoza zakresu nie są pobierane,
   więc nie wpływają na ranking ani `has_more`/`complete` (`search.py:106-141`).
3. **Fragmenty, cytaty, przypisania, relacje, metadane** pobierane przez `ContentGate.get(kind, id,
   capability)`; zwraca `Denied` (nie `NotFound`-vs-`Forbidden` rozróżniane dla odbiorcy, by nie
   ujawniać istnienia; tryb rozróżniający to ustawienie, 2.9).
4. Do promptu trafia **tylko to, co przeszło bramkę**, razem z `basis` (lista uid+wersji). Ostateczny prompt
   jest zapisywany (w sensie metadanych: lista uid, hashe) w `advocate_turns`.
5. Odpowiedź modelu przechodzi **weryfikator cytatów**: każde odwołanie (`[cit:uid]`) musi istnieć w
   `basis` i spełniać `visible()`; odwołania spoza `basis` są usuwane/oznaczane, odpowiedź nie jest
   oddawana z „twierdzeniami bez podstawy” bez oznaczenia (stan: *dowiedziony / twierdzenie /
   hipoteza / nieznane* jako pole, zgodnie z kierunkiem „proven facts and hypotheses”; rozróżnienie jest
   etykietą w danych, nie liczbową pewnością — `AGENTS.md`).
6. Odbiorca sprawdza podstawę odpowiedzi: `GET advocate/answers/{uid}/basis` zwraca cytaty/fragmenty z
   `basis` **przez ten sam `visible()`**; nic poza zakresem nie jest dołączane.

**Zapisane stany pochodne** (to właśnie źródła „wycieku przez odpowiedź pochodną”), każdy z kluczem
`derived_scope`:

| Stan | Reguła |
|---|---|
| Streszczenie / analiza / odpowiedź zapisana (`derived_text`, `advocate_answers`) | zapisuje `input_refs_json`; `derived_scope` = **przecięcie** zakresów wszystkich wejść. Widoczna dla principala tylko jeśli jego zakres zawiera `derived_scope` (wszystkie wejścia widoczne). Inaczej niewidoczna; „analiza oparta na materiałach spoza uprawnień nie jest udostępniana automatycznie” |
| Cache (embeddingi, wyniki wyszukiwania, odpowiedzi) | klucz cache zawiera `scope_key` (skrót zbioru uprawnień efektywnych); cache jest per `(scope_key, wersja podstawy)`; cofnięcie/zmiana grantu unieważnia przez zmianę `scope_key` (generacja grantów w kluczu) |
| Indeks (FTS / wektorowy) | wiersz ma `scope_key` lub listę zakresów; filtr w zapytaniu. Wariant alternatywny (osobny indeks per zakres) zostaje jako ustawienie, kosztowny, dla silniejszej izolacji |
| Pamięć rozmowy | należy do `(principal, conversation_id)`; przechowuje **referencje i `basis`**, a nie wklejoną treść; przy każdej turze referencje są **ponownie** autoryzowane; po cofnięciu dostępu stare tury pokazują „materiał niedostępny”, a treść nie wraca do promptu |
| Eksporty | tylko `export_scope_package` (2.6) lub eksport rozmowy zbudowany z ponownie autoryzowanego `basis` |
| Logi / audyt | `audit_log.details_json` dla odbiorcy nie jest zwracany; wpisy o obiektach niedostępnych nie zawierają nazw (`actor` = principal uid) |
| Telemetria dostawcy modelu | to, co wysyłamy do dostawcy, jest dokładnie promptem po bramce; zapisujemy hash i listę uid per turę (nowy `provider_calls`, bez treści, chyba że ustawienie `log_prompt_content`) |

Odrębność **lokalnych cache klienta Android** (`BoundedMetadataCache`, `SourceScanCache`): klient po zmianie
konta/cofnięciu grantu czyści cache należący do zakresu; zakres cache jest w kluczu. [PLANOWANE, pakiet P8]

**Dostawca modelu jest ustawieniem**: `advocate_providers` (nazwa, typ API, endpoint, klucz w sekretach poza
repo, `data_policy_json`). Domyślnie **żaden dostawca nie jest włączony** i rzecznik działa w trybie
`retrieval_only` (zwraca przefiltrowane fragmenty, cytaty i metadane bez generowania tekstu). Włączenie
dostawcy wymaga zapisanego, jawnego `provider_consent` właściciela dla konkretnych `scope_id` — to jest
**pytanie tak/nie do właściciela** (pkt 7), nie decyzja w tym dokumencie. Silnik obsługuje też dostawcę
lokalnego (ten sam interfejs). Szacunek kosztu przed uruchomieniem jest ustawieniem; limity są
presetami, nie regułami (z wyjątkiem reguły ×10 z `CLAUDE.md` ChatADHD).

### 2.9 Nierozstrzygnięte jako jawne ustawienia

Każde ma wartość domyślną (preset), zakres obowiązywania (instalacja / pakiet / grant / sprawa) i regułę
„brak milczącego rozszerzania”: **zmiana ustawienia nigdy nie poszerza już udzielonego dostępu bez zapisu
w audycie i wyświetlenia właścicielowi różnicy** (`effective_access_diff`).

| Pytanie z wymagania | Ustawienie | Wartości (otwarte, przykłady) | Preset startowy |
|---|---|---|---|
| Pakiet na bieżąco czy snapshot | `package.mode` (per pakiet, nadpisanie per element) | `live`, `snapshot`, `snapshot_at:T`, `live_for:<capability>` | `snapshot` (węższy; to preset, odwracalny jedną zmianą) |
| Czy odbiorca może pobierać oryginały | zdolność `read_bytes` w grancie | dowolny podzbiór zdolności | brak |
| Adnotować / proponować korekty / zapraszać | zdolności `annotate`, `propose`, `invite`; `invite` ma `max_delegation_depth` (liczba, `null` = bez limitu) | j.w. | brak |
| Czy rozmowa odbiorcy z rzecznikiem jest widoczna dla właściciela | `conversation.owner_visibility` (per grant/pakiet) | `none`, `metadata_only`, `full`, `opt_in_by_recipient`, `custom:<predykat>` | `none`; odbiorca **widzi to ustawienie** przed rozmową (przejrzystość) |
| Czy fragment otwiera cały dokument | `fragment.open_policy` | `fragment_only`, `context_window:N`, `whole_document`, `whole_artifact_version`, `predicate` | `fragment_only` |
| Jak nowe materiały wpływają na istniejące udostępnienia | `scope.growth_policy` | `frozen`, `auto_include_by_rule:<predykat>`, `propose_to_owner`, `notify_only` | `frozen` + powiadomienie |
| Odróżnialność „nie istnieje” / „brak dostępu” | `denial.reveal_existence` | `never`, `category_only`, `always` | `never` |
| Ujawnianie liczności (np. „3 inne dokumenty”) | `count_disclosure` | `none`, `bucketed`, `exact` | `none` |
| Poprzednie wiersze bez zakresu (sprzed modelu) | `legacy_scope_policy` | `owner_only`, `treat_as_case:<id>`, `explicit_map` | `owner_only` |
| Tryb bez kont | `auth.mode` | `legacy_open`, `tokens`, `external` | `legacy_open` do pakietu P3, potem `tokens` jeśli istnieje ≥1 grant poza właścicielem (doradczo) |
| Czy wspólny materiał uwidacznia istnienie *innych* spraw | `shared_source.cross_case_hint` | `never`, `own_cases_only` | `never` |

„Preset” znaczy tu wartość początkowa w danych; żadne z nich nie jest przypisywane właścicielowi jako jego
decyzja. Wszystkie wartości muszą mieć testy silnika (pkt 3), także te mało użyteczne.

## 3. Plan testów

Zasada: przykłady konieczne, niewystarczające. Obok scenariuszy — testy własności na losowych
konfiguracjach (hypothesis); luki w pokryciu są raportowane jako luki. Dane wyłącznie syntetyczne.

### 3.1 Sześć scenariuszy z „Weryfikacja” (wymagane minimum)

| # | Scenariusz | Sprawdzenie (kryterium) |
|---|---|---|
| S1 | Jeden materiał w dwóch sprawach | Po przypisaniu do spraw A i B: `COUNT(artifacts)` bez zmiany; jeden `uid`, jeden `sha256`; dwa `assignments` z różnym `meaning_text`/autorem/statusem; zapytania „po sprawie” zwracają ten sam uid |
| S2 | Usunięcie (wycofanie) jednego przypisania | Drugie przypisanie, źródło, `derived_text`, `evidence_anchors` bez zmian (porównanie hashy wierszy przed/po); twarde usunięcie również; brak kaskady w stronę materiału |
| S3 | Odbiorca sprawdza podstawę odpowiedzi | Odpowiedź ma `basis`; każdy cytat otwiera się przez trasę odbiorcy w udostępnionym źródle i ma ten sam `quote_sha256`; cytat poza `basis`/zakresem jest usunięty |
| S4 | Niedostępna sprawa i jej prywatne metadane przez wspólny materiał | Odbiorca z dostępem do A (materiał X wspólny z B) nie dostaje: przypisań B, adnotacji właściciela, linków B, tagów jedynie z B, nazwy sprawy B, wpisów audytu, liczności (przy `count_disclosure=none`) |
| S5 | Brak ujawnienia treści spoza uprawnień przez wyszukiwanie, podsumowania, cache, historię | Wstrzyknięte „znaczniki kanarkowe” w materiale spoza zakresu; dla wszystkich ścieżek (search, summary, cache hit po wcześniejszym pytaniu właściciela, pamięć rozmowy, eksport zakresowy, błąd/log) kanarek nie występuje w żadnym bajcie wyniku ani promptu wysłanego do dostawcy (atrapa dostawcy zapisuje prompty) |
| S6 | Aktualizacje zachowują wersję podstawy odpowiedzi | Po nowej wersji bajtów/transkrypcji wcześniejsza odpowiedź nadal wskazuje stare `artifact_id+sha256+derived_text_id`; „odpowiedz ponownie” tworzy nową odpowiedź z nową podstawą; stara nie jest nadpisana (append-only) |

### 3.2 Dodatkowe scenariusze wynikające z wymagania

S7 cofnięcie grantu w trakcie rozmowy (następna tura nie ma treści; kolejne zapytanie z cache nie trafia);
S8 analiza oparta częściowo na materiałach spoza zakresu odbiorcy jest niewidoczna dla niego, widoczna dla
właściciela; S9 materiał bez bajtów (referencja zewnętrzna) działa w całym przepływie; S10 dwa wystąpienia
tego samego materiału w pakiecie liczone jako jedno źródło niezależności; S11 `snapshot` vs `live` po dodaniu
materiału (każdy tryb i `growth_policy`); S12 `fragment.open_policy` w każdej wartości; S13 każde ustawienie z
2.9 w każdej wartości (macierz), w tym „mało użyteczne”; S14 tryb `legacy_open` zachowuje dzisiejsze zachowanie
(regresja istniejących testów `server/tests/*`); S15 cały przepływ w trybie `retrieval_only` bez dostawcy
(żadne wywołanie sieciowe — test z blokadą gniazd).

### 3.3 Testy własności (property-based)

Generator `Config`: losowe grafy (principals, sprawy, materiały z wieloma wersjami/fragmentami/pochodnymi,
przypisania, pakiety, granty z oknami czasu i cofnięciami, ustawienia z 2.9), z osadzonymi kanarkami w każdym
polu tekstowym. Własności:

- **P1 (brak wycieku, główna):** dla każdego principala *p* i każdej ścieżki odczytu *r* (search, get,
  basis, summary, export, conversation replay, cache, błędy): `canaries(output) ⊆ canaries(visible(p))`.
- **P2 (monotoniczność):** dodanie grantu nie zmniejsza dostępu; cofnięcie nie zwiększa żadnego wyniku.
- **P3 (izolacja):** przy zmianie wyłącznie danych spoza zakresu *p* wynik dla *p* jest identyczny
  bajt-w-bajt (nieinterferencja; obejmuje ranking i `has_more`).
- **P4 (derived_scope):** zapisany stan pochodny jest widoczny dla *p* wtedy i tylko wtedy, gdy wszystkie
  jego wejścia są widoczne dla *p*.
- **P5 (cache):** wynik z cache = wynik bez cache dla każdej pary (principal, generacja grantów).
- **P6 (niezależność pakietu):** liczba niezależnych źródeł pakietu = liczba unikalnych uid źródeł,
  niezależnie od liczby przypisań.
- **P7 (odwracalność/addytywność):** wycofanie przypisania nie zmienia hashy wierszy źródła, wersji, cytatów.
- **P8 (migracja):** `migrate_db` na losowej starej bazie nie zmienia istniejących wierszy; `legacy_scope_policy`
  daje zadeklarowany efekt.
- **P9 (zgodność z paczką):** eksport zakresowy waliduje się istniejącym `validate_metadata_package` i nie
  zawiera wierszy poza zakresem.

Dodatkowo: test statyczny (lint), że żadna trasa/serwis rzecznika nie czyta tabel treści poza `ContentGate`
(wzorzec: istniejące `scripts/verify_repository.py`); test kontraktowy atrapy dostawcy (zapisuje wszystko,
co dostał). Zakres „zgodnie z AGENTS.md”: podać faktycznie uruchomione sprawdzenia, nie obiecywać Androida
bez builda.

## 4. Pakiety implementacji (małe, ok. 50 wywołań narzędzi każdy)

Kolejność: model danych i autoryzacja -> rzecznik (wyszukiwanie z uprawnieniami) -> interfejs rozmowny.
Każdy pakiet kończy się commitem, kartą wznowienia i uruchomionymi testami. Wszystko na gałęzi roboczej,
bez dotykania `server/data`; testy na danych syntetycznych.

| ID | Pakiet | Zawartość | Kryteria odbioru | Blokuje / zależy |
|---|---|---|---|---|
| **P1** | Model danych bazowy | migracja addytywna: `uid` na `cases/artifacts`, `principals`, `assignments`, `fragments`, `artifact_versions`, `case_relations`; rozszerzenie `links` | schemat przechodzi `migrate_db` na starej bazie bez zmiany wierszy (P8); S1, S2 zielone; istniejące `server/tests` bez regresji | start; blokuje P2–P12 |
| **P2** | Uprawnienia i `authz.py` | `scopes`, `grants`, `capability_registry`, `visible()`, `legacy_scope_policy`, `default_deny`, audyt zmian grantów | P2, P3 (własności monotoniczności i izolacji) na modelu bez HTTP; macierz ustawień 2.9 dla zdolności | P1; blokuje P3–P12 |
| **P3** | Uwierzytelnianie i bramka HTTP | `auth_methods`, tokeny hashowane, `Depends` na trasach, tryb `legacy_open` jawny, zwrot `Denied` wg `denial.reveal_existence` | każda istniejąca trasa przechodzi przez `visible()` (test statyczny); S14 regresja; brak tras omijających | P2; blokuje P4–P12 |
| **P4** | Wyszukiwanie z zakresem | `scope_key` w indeksie FTS (migracja + backfill), filtr w zapytaniu, spójność `has_more/complete` | P1 (kanarki), P3 (nieinterferencja) dla wyszukiwania; S5 część „search” | P3; blokuje P6 |
| **P5** | Pakiety i eksport zakresowy | `packages`, `package_items`, `mode`, `export_scope_package`, niezależność źródeł | S10, S11, P6, P9; eksport administracyjny oznaczony i tylko dla właściciela | P3 (może równolegle z P4) |
| **P6** | Rdzeń rzecznika (bez modelu) | `ContentGate`, `basis`, `derived_scope`, `advocate_turns/answers` append-only, tryb `retrieval_only`, weryfikator cytatów | S3, S4, S6, S8, S15; P4 (derived_scope) | P4; blokuje P7–P12 |
| **P7** | Cache, pamięć rozmowy, stany pochodne | klucze z `scope_key`+generacja, referencje zamiast treści w pamięci, ponowna autoryzacja, unieważnianie | S5 (cache/historia), S7, P5 (cache) | P6 |
| **P8** | Adapter dostawcy modelu (ustawienie) | interfejs `Provider`, atrapa nagrywająca prompt, lokalny dostawca, `provider_consent`, szacunek kosztu; **żaden dostawca nie jest włączony domyślnie** | kontrakt atrapy: prompt = tylko po bramce; S5 pełne; brak sieci bez zgody (test blokady gniazd) | P6, P7 |
| **P9** | Ustawienia 2.9 — pełna macierz | `settings` z zakresem, `effective_access_diff`, doradcze ostrzeżenia, „nie pytaj ponownie” | S12, S13 w pełnej macierzy; zmiana ustawienia nie poszerza dostępu bez zapisu diff | P2, P5, P6 |
| **P10** | Interfejs tekstowy rozmowy | trasa/strona czatu na koncie odbiorcy, widok podstawy, widoczność rozmowy zgodnie z `owner_visibility` | S3 end-to-end w przeglądarce (istniejący harness Playwright `server/tests/browser/`) | P8, P9 |
| **P11** | Android: konta, zakres, cache | token, czyszczenie cache przy zmianie konta/cofnięciu, ekran rozmowy tekstowej | testy JVM; oddzielnie zadeklarowany brak/obecność builda SAF (AGENTS.md) | P3, P10 |
| **P12** | Głos, potem awatar | wejście/wyjście głosowe jako warstwa nad tym samym API; awatar jako osobny pakiet | te same bramki co tekst (głos nie omija `ContentGate`); awatar oznaczony jako **PLANOWANE** do chwili dostarczenia | P10 |

**Stan P1 (2026-10-25, gałąź `claude/rzecznik-design`):** wykonane. Migracja addytywna w `server/app/db.py`
(`EVIDENCE_MODEL_SCHEMA`, `schema_migrations` v2), operacje w `server/app/assignments.py`, testy
`server/tests/test_assignments_model.py` (S1, S2, P8 przykładowo — bez testów własności hypothesis). Decyzje:
wiersze sprzed migracji mają `uid NULL` i dostają uid przy pierwszym użyciu (`ensure_uid`, wpis audytu), nowe
wiersze dostają uid triggerem; `fragments` i `artifact_versions` są append-only; cel, sprawa i autor przypisania są
niezmienne (trigger), status i `withdrawn_at` zmienialne z audytem bez treści `meaning_text`. **Luka:** nowe tabele
(`principals`, `assignments`, `fragments`, `artifact_versions`, `case_relations`) nie wchodzą do
`export_metadata_package` v1 ani archiwum (stały zestaw `TABLES`); nowe kolumny tak. Rozszerzenie paczki to osobna
wersja schematu (P5). Brak tras HTTP — celowo, do P3.

**Stan P2 (2026-10-25):** wykonane. `server/app/authz.py` (`homes`, `coverage`, `visible`, `ContentGate`,
`bootstrap_owner`, `set_setting` z audytem poprzedniej wartości), migracja v3 (`scopes`, `grants`,
`capability_registry`, `authz_settings`, `auth_methods`). Rodzaje zakresu: `installation`, `case`, `owned_cases`,
`set`, `legacy_unscoped`; inny rodzaj zostaje w danych i nic nie pokrywa. Granty niezmienne poza jednym cofnięciem
(trigger), zakresy niezmienne. Testy `server/tests/test_authz.py`: P1 (kanarki + porównanie z niezależną wyrocznią),
P2 (monotoniczność), P3 (nieinterferencja) po 150 losowych konfiguracji hypothesis każda, oraz S4 i scenariusze
okien czasu/cofnięcia/nieznanych zdolności. Pięć celowych mutacji `authz.py` wykrytych przez testy. **Granica:**
`ContentGate.list` filtruje w Pythonie po pobraniu wierszy (poprawność, nie wydajność); wyszukiwanie FTS jeszcze
globalne (P4); adnotacje, tagi i audyt są bez autora/zakresu, więc zawsze idą przez `legacy_scope_policy`.

**Stan P3 (2026-10-25):** wykonane. `server/app/auth.py` (tokeny `<uid>.<sekret>`, sól + SHA-256, parametry w
danych, pokazywane raz), `server/app/http_authz.py` (dokładnie jeden strażnik na trasę: `installation:<zdolność>`,
`artifact:<zdolność>`, `scoped:<zdolność>` z filtrem przez `ContentGate`, `authenticated`), CLI `authz-*`.
`auth.mode=legacy_open` pozostaje domyślne (nagłówek `X-AGEDS-Auth-Mode`, ostrzeżenie
`X-AGEDS-Auth-Warning: recipient_grants_unenforced`, gdy istnieją granty odbiorców); nieznany tryb nikogo nie
wpuszcza; `authz-mode tokens` odmawia bez tokenu właściciela. Token: nagłówek `X-AGEDS-Token` (zgodny z basic auth
proxy na VM), `Authorization: Bearer` albo cookie `ageds_token`. W trybie `tokens` odbiorca bez zakresu instalacji
**nie ma** (403): wyszukiwania (do P4), eksportów i paczki cytatu (do P5 — paczka zawiera źródło i pozyskania),
osi czasu, stron HTML, adnotacji (bez autora) ani zapisów poza cytatem (`cite` + `read_text`). Ma: listę materiałów
(filtr przed limitem), transkrypcje, cytaty, wersje, pozyskania z własnej sprawy, bajty tylko z `read_bytes`.
Odmowa dla ukrytego i nieistniejącego obiektu jest bajtowo identyczna przy `denial.reveal_existence=never`. Testy
`server/tests/test_http_authz.py`: statyczny (każda trasa ma jednego strażnika), 401 na każdej trasie dla braku/złego
tokenu, kanarki przez wszystkie trasy GET, cofnięcie grantu i tokenu, S4 przez HTTP; pięć celowych mutacji wykrytych.
S14: wszystkie wcześniejsze testy bez zmian w `legacy_open`; przeglądarka 24/24 przed i po zmianie.

**Stan P4 (2026-10-25):** wykonane, wariant „indeks per zakres” budowany na żądanie zamiast kolumny `scope_key`
w globalnym FTS. Powód: bm25 liczy statystyki z całego indeksu, więc filtr w `WHERE` na globalnym indeksie zmienia
ranking widocznych wyników zależnie od ukrytych dokumentów (narusza P3). `scoped_search_page` buduje tymczasowy FTS5
w pamięci tylko z wierszy przepuszczonych przez `ContentGate` (`read_text`) i wykonuje na nim to samo zapytanie;
właściciel (zakres instalacji) zostaje na dotychczasowej ścieżce. Koszt O(widoczne dokumenty) na zapytanie, limit
50 000 dokumentów (`SearchWorkLimitError`); cache per `Coverage.key()` to P7. Testy `test_scoped_search.py`: wynik ==
wyszukiwanie w instalacji zawierającej tylko widoczne dokumenty (z rankingiem i `has_more`), dodanie ukrytych
dokumentów z tymi samymi słowami nic nie zmienia (hypothesis, 40 przykładów); mutacja „globalny indeks + filtr po
wynikach” wykryta.

**Stan P5 (2026-10-25):** wykonane. Migracja v4 (`packages`, `package_items`, `package_snapshots` append-only),
`server/app/sharing.py`, zakres `package` w `authz.coverage`, trasa `GET /api/export/scoped`, CLI `authz-package-*`.
Decyzje: liczy się tylko najnowszy snapshot; jawne `authz-package-snapshot` może poszerzyć (audyt), a wycofanie
elementu zapisuje nowy snapshot = poprzedni ∩ nadal osiągalne (tylko zawęża). Dom obiektu rozdzielony na `object`
(sam obiekt) i `container` (obiekt zawierający): zamrożony artefakt nie obejmuje późniejszych wersji tekstu ani
cytatów, grant `set`/element `live` obejmuje. Eksport zakresowy ma schemat `ageds.metadata-package/v1`, przechodzi
`validate_metadata_package`; referencje do ukrytych wierszy są zerowane i wypisane w `redacted_references` (bez
wartości), `stored_path` i tokeny lease też (`installation_internal`); audyt i tagi pominięte, liczności nie
ujawniane. Grupa niezależności = `sha256`, inaczej `uid`. Testy `test_sharing.py` (S10, S11, P6 hypothesis, P9);
trzy celowe mutacje wykryte.

**Stan P6 (2026-10-25):** wykonane. Migracja v5 (`conversations`, `advocate_turns`, `advocate_answers`,
`derived_states`; trzy ostatnie append-only), `server/app/advocate.py`, trasy `/api/advocate/*`. `AdvocateGate` =
przecięcie `read_text` i `chat_with_advocate`. Pobieranie: zapytanie z pytania (OR prefiksów słów ≥3 znaków, prefiks
skrócony o 3 znaki dla fleksji) → `scoped_search_page` → segmenty transkrypcji zawierające słowa (albo okno tekstu),
zdarzenia, a dla cytowanego materiału jego przypisania w widocznych sprawach (oznaczone jako interpretacja). Każdy
wpis `basis` przypina `artifact.sha256`, `derived_text_id`, segment i `text_sha256`. Odpowiedź = lista twierdzeń z
etykietą statusu (`quoted_material`, `proven`, `claim`, `hypothesis`, `unknown`, `claim_without_basis`; etykiety, nie
prawdopodobieństwa) i cytatami do `basis`; weryfikator usuwa cytaty spoza `basis` lub już niewidoczne, nie podnosi
statusu, `proven` bez cytatu → `claim_without_basis`. Rozmowa ma dom `private` (zakres instalacji jej nie otwiera;
`conversation.owner_visibility=full` otwiera, preset `none`). Odpowiedź i `derived_state` wymagają widoczności
wszystkich wejść (`require_inputs`). Bez dostawcy: `retrieval_only` (bez generowania). Testy `test_advocate.py`:
S3, S4, S6, S7, S8, S15 (blokada gniazd), weryfikator, statyczny (moduł nie czyta tabel treści), HTTP end-to-end; 4
celowe mutacje wykryte. Granica: dopasowanie słów jest leksykalne (bez lematyzacji i embeddingów).

**Stan P7 (2026-10-25):** wykonane. Cache widocznych dokumentów wyszukiwania (`search._visible_documents`) z kluczem
(tożsamość pliku bazy: ścieżka+inode+urządzenie, rodzaj bramki, principal, `Coverage.key()` dla `read_text` i
`chat_with_advocate`, `authz.data_generation`); baza w pamięci nie jest cache'owana; maks. 16 wpisów. Generacja =
liczności i maksima tabel wpływających na widoczność + skrót kolumn zmiennych (wycofania, cofnięcia, statusy,
ustawienia). Wyszukiwanie z zakresem czyta dokumenty przez połączenie bramki (jeden stan bazy). Pamięć rozmowy: od P6
referencje i `basis`, ponowna autoryzacja przy każdym odczycie (S7). Testy `test_search_cache.py`: P5 (wynik z cache ==
bez cache po każdej z losowych operacji, hypothesis 60 ciągów), cache użyty i niewspółdzielony. Mutacja „bez generacji”
wykryta; usunięcie principala lub cofnięć z klucza jest maskowane redundancją `Coverage.key` (świadoma obrona w głąb).
Pomiar (syntetyczny korpus): 8000 dokumentów, pierwsze zapytanie 0,4–1,2 s.

**Stan P8 (2026-10-25):** kod i testy na atrapie wykonane; **prawdziwe wywołanie Vertex NIE zostało jeszcze
wykonane** (próba w sesji zablokowana przez kontrolę uprawnień; czeka na zgodę właściciela sesji). `server/app/providers.py`:
dostawca = ustawienie `advocate.provider` (preset Vertex `gemini-3.5-flash`, `global`, projekt z metadata servera,
token z metadata servera albo `gcloud`), domyślnie brak → `retrieval_only`. Wysłanie wymaga: zapisanej, niecofniętej
zgody (`provider_consents`, zakres) pokrywającej **każde** wejście `basis` (inaczej powrót do `retrieval_only` z powodem),
oraz zmieszczenia się w `advocate.daily_budget_eur` (preset 1,0) wraz z najgorszym przypadkiem wywołania. Prompt =
instrukcja systemowa + przefiltrowane fragmenty + ponownie autoryzowana historia (odpowiedzi z niedostępnym materiałem
nie wracają). Wynik w JSON wg `responseSchema`, potem weryfikator z P6. `provider_calls` (append-only): hash promptu,
referencje wejść, tokeny, koszt (cena z danych; brak raportu zużycia → najgorszy przypadek, nigdy zero), treść tylko
przy `advocate.log_prompt_content=true`. Ceny presetu: 1,50 / 9,00 USD za 1M tokenów wejścia/wyjścia — z zewnętrznych
agregatorów, niezweryfikowane u Google; `eur_per_usd` 1,0 zawyża koszt. Zgoda R100 (właściciel, 2026-10-25) uprawnia
do zapisu zgody na zakres instalacji; zapis wykonuje właściciel poleceniem CLI (niżej), nie kod domyślnie. Testy
`test_providers.py` (atrapa nagrywająca): bez zgody/przy złej zgodzie/przekroczonym budżecie nic nie jest wysyłane;
prompt zawiera tylko materiał po bramce (kanarki), historia ponownie autoryzowana, koszt i błędy księgowane.

**Stan P10 (2026-10-25):** wykonane (tekst; głos i awatar to P12, nadal PLANOWANE). Strona `/advocate`
(`templates/advocate.html`, `static/advocate.mjs`, `static/advocate.css`): logowanie tokenem → cookie `ageds_token`
(HttpOnly, SameSite=Strict, Secure za https), lista rozmów, pytanie, twierdzenia z etykietą słowną, cytaty rozwijane do
przypiętej podstawy (rodzaj, plik, zakres czasu, model tekstu, SHA-256, wersja tekstu), odsłuch zakresu tylko przy
`read_bytes`, informacja o widoczności rozmowy dla właściciela i o tym, czy model jest włączony. `GET /api/advocate/status`.
Trasy publiczne (bez odczytu danych) to zamknięta lista `/advocate`, `/advocate/login`, `/advocate/logout` (test). Dane ze
źródła wyłącznie przez `textContent`. Odbiór w prawdziwym Chromium: `server/tests/browser/advocate.cjs` (7 scenariuszy:
logowanie, zły token, pytanie, cytat, odsłuch kończy się na końcu zakresu, historia po przeładowaniu, brak kanarka, brak
wykonania znaczników ze źródła, wylogowanie) na syntetycznym WAV i transkrypcji; dawny zestaw przeglądarki 24/24.

Równoległość: P4 i P5 mogą iść równolegle po P3 (inne pliki); reszta sekwencyjnie. Każdy pakiet: krótki brief
(pliki, decyzje, stan), commit pośredni po każdym kawałku, bez zależności od kontekstu sesji.

Realne materiały właściciela: żaden pakiet nie ładuje danych prywatnych w testach; podpięcie realnego korpusu
to osobny krok właściciela (import przez istniejące mechanizmy, `server/data` poza repozytorium), a
funkcje niegotowe są w UI oznaczone jednoznacznie („planowane”).

## 5. Podział: istnieje / proponowane / planowane

| Obszar | JEST (działa, ma testy w repo) | PROPONOWANE (ten dokument) | PLANOWANE (pakiet) |
|---|---|---|---|
| Sprawy, źródła, artefakty, pozyskania, runy, cytaty, adnotacje, linki, FTS, paczki metadanych, archiwum, paczka cytatu, kolejka z lease, klient Android korpusu/cytatów | tak (pkt 1) | — | — |
| Wiele-do-wielu materiał↔sprawa, przypisanie z metadanymi, fragmenty, wersje materiału, referencje zewnętrzne, relacje spraw | nie | pkt 2.3–2.4 | P1 |
| Konta, granty, zakresy, `visible()` | nie | 2.2, 2.5 | P2, P3 |
| Pakiet zakresowy, tryby live/snapshot, eksport zakresowy | nie (jest tylko eksport całej bazy) | 2.6 | P5 |
| Wyszukiwanie z zakresem | nie (globalny FTS) | 2.8 | P4 |
| Rzecznik: bramka treści, `basis`, `derived_scope`, tryb `retrieval_only` | nie | 2.8 | P6 |
| Cache/pamięć rozmowy z zakresem | nie | 2.8 | P7 |
| Dostawca modelu jako ustawienie | nie | 2.8 | P8 |
| Ustawienia nierozstrzygnięte | nie | 2.9 | P9 |
| Rozmowa tekstowa / głos / awatar | nie | 2.8 | P10 / P12 |
| Klient Android z kontami | nie | — | P11 |

## 6. Ryzyka i luki jawnie raportowane

- Wyszukiwanie z filtrem zakresu w FTS5: backfill i rozmiar indeksu przy wielu zakresach do zmierzenia w P4;
  wariant „indeks per zakres” zostaje opcją.
- Istniejące uwagi w polach tekstowych (`details_json`, `metadata_json`, `label`, `original_name`) mogą zawierać
  nazwy ze spraw; w P2/P4 każda taka kolumna trafia do rejestru „pól wrażliwych na zakres” testowanego
  kanarkami — lista nie jest zamknięta, a niepełne pokrycie będzie raportowane jako luka.
- Dostęp do `stored_path` (system plików) musi przejść przez te same bramki; skan nie zmienia źródeł.
- Dostawca zewnętrzny widzi to, co wysłano; cofnięcie dostępu nie cofa wysłanych promptów ani pobranych kopii
  (informacja w UI przy udzielaniu).
- Hash/anchor nie dowodzą prawdy wypowiedzi ani autorstwa (AGENTS.md); rzecznik nie przedstawia ich jako
  dowodu prawdy.

## 7. Pytania tak/nie do właściciela (tylko te, które wolno zadać)

1. Czy wolno wysłać do konkretnego dostawcy modelu (wybranego w ustawieniu) przefiltrowane fragmenty
   materiałów określonego zakresu (np. pakiet dla Wouta)? Domyślnie: nie; rzecznik pracuje w trybie
   `retrieval_only`, dopóki nie zapisze zgody (`provider_consent`).
2. Czy podłączyć realny korpus (import przez istniejące mechanizmy) dopiero po P3 (konta), a nie wcześniej?
   Preset: tak, bo przed P3 nie ma izolacji.

Pozostałe sprawy techniczne rozstrzygnięte presetami w 2.9 (odwracalne zmianą wpisu w ustawieniach; rejestr
decyzji: ten plik, sekcja 2.9).
