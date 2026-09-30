# AGEDS — niezależny odbiór nocnego przyrostu

N6 · noc 30 września / 1 października 2026, Europe/Amsterdam.
Baza przydziału: `b22881eb5a60f662fb6e14153e1fbe338d6ab687`.
Ostateczny opublikowany commit wskazuje rekord koordynatora; poniższe liczby
dotyczą wykonanych przebiegów, nie każdej przyszłej wersji repozytorium.

**W sprawdzonym zakresie backendu nie pozostał znany bloker.** Niezależny
przepływ obejmuje rzeczywiste pozyskanie syntetycznych bajtów, kolejkę i worker
z atrapą ASR, dwie wersje transkryptu, cytat słowny ze starszej wersji oraz
eksport → izolowane archiwum SQLite → ponowny eksport metadanych.

## Dowody wykonania

| Przebieg | Wynik | Faktyczny zakres |
|---|---|---|
| Baseline `python -m pytest server/tests -q` | 126 testów + 84 podtesty przeszły; 3 ostrzeżenia | Potwierdzenie fundamentu po odzyskaniu środowiska. |
| Niezależny wspólny backend N6, ta sama komenda | 177 testów + 194 podtesty przeszły, 3 ostrzeżenia, 6,16 s | Nowe cytaty, archiwum, CLI, API oraz wcześniejsze niezmienniki. |
| Końcowy backend wykonany przez koordynatora po ochronie przed powtórzonymi kluczami JSON | **180 testów + 207 podtestów przeszło**, 3 ostrzeżenia, 6,95 s | Pełny zakres powyżej oraz trzy dodatkowe testy odrzucania niejednoznacznego JSON. N6 przyjmuje raport koordynatora; nie powtarzał tego przebiegu. |
| Końcowa regresja po poprawce renderowania wyników, koordynator | **182 testy + 207 podtestów przeszły**, 3 ostrzeżenia, 8,81 s | Pełny backend po potwierdzonym i naprawionym XSS we fragmentach FTS. |
| Niezależny `server/tests/test_night_integration.py` | 7 testów + 14 podtestów, zawarte w powyższym przebiegu | Przepływy przekraczające granice modułów i regresje znalezione w przeglądzie. |
| Moduł cytatów i odtwarzania, Node | **14/14 testów przeszło**, wynik przekazany przez koordynatora | Atrapa audio sprawdza sterowanie odtwarzaniem i selekcją; nie jest to odsłuch ani pomiar przeglądarki na urządzeniu. |
| Rzeczywisty końcowy build Android, N5 | **57/57 zadań Gradle; 16 testów modułu Android i 10 desktop przeszło**, bez błędów i pominięć | Debug APK oraz lokalne testy JVM; `source_unchanged=true`. Szczegóły i hashe: [`ANDROID_NIGHT_BUILD_RECEIPT.json`](ANDROID_NIGHT_BUILD_RECEIPT.json). |
| Native skaner i Android UI | Przegląd kodu; uwagi przekazane właścicielom i poprawione | Granice wejścia, zachowanie raw pól, ograniczanie pamięci, anulowanie i cache. To nie jest test urządzenia. |

Ostrzeżenia pochodzą z dotychczasowego `on_event` FastAPI oraz aliasu
`BlockingPortal` AnyIO w TestClient. Nie są błędami wykonania.

Środowisko sprawdzenia: Python 3.12.14, zależności
`server/requirements-test.txt`, zainstalowane poza repo. Komenda użyta w tej sesji:

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/workspace/scratch/773725428b88/review/deps:/workspace/scratch/773725428b88/AGEDS \
python -m pytest server/tests -q
```

Ścieżki środowiska są odtwarzalne i tymczasowe. Nie są zależnością projektu;
w zwykłym virtualenv po instalacji wymagań wystarcza `python -m pytest server/tests -q`.

## Sprawdzone przepływy i kontrprzykłady

- Pozyskanie zachowuje bajty i czas modyfikacji syntetycznego oryginału.
  Worker rzeczywiście odczytuje zachowaną kopię; atrapę modelu jawnie opisują
  metadane runu. Dwie zakończone próby dają dwa runy i dwie wersje.
- Cytat przechodzi przez granicę dwóch segmentów starszej wersji. Zachowuje
  polskie znaki, podwójne spacje i znak nowego wiersza, hash UTF-8, surowy czas
  słów i zaokrąglenie przedziału do milisekund. Nowsza wersja nie zmienia cytatu.
- Trzeci job pozostaje aktywny w żywej bazie. Archiwizacja zachowuje jego
  historyczne metadane, nie zwalnia lease, nie tworzy wykonywalnej tabeli jobs
  ani nie uruchamia pracy. Archiwum zawiera wyłącznie własne tabele koperty
  i rekordów. Ponowny eksport ma identyczną kanoniczną reprezentację pakietu,
  włącznie z czasem eksportu i digestami.
- Zmiana precyzji, surowego czasu lub indeksów selektora jest wykrywana także
  po przeliczeniu wszystkich digestów pakietu. Boolean nie jest indeksem 0/1.
- Brak słów, błędne słowa lub NaN w nieużytych słowach nie odbierają ważnego
  wyboru segmentowego. Nie powstaje cytat słowny. Historyczny `segments_json`
  pozostaje literalnym polem; nie zmieniamy ani nie normalizujemy go w archiwum.
- HTTP upload → kolejka → worker → wybór starszej wersji → `wordRefs` → eksport
  zachowuje tę samą tożsamość artefaktu i transkryptu. Dwa selektory naraz,
  brak selektora, inny tekst, dodatkowe pole lub boolean dają 422 bez zapisu.
- HTML artefaktu zawiera zapisany cytat i numer jego wersji oraz odnośnik do
  modułu odtwarzania. TestClient sprawdza render i dostępność modułu; nie
  wykonuje JavaScriptu ani nie mierzy odtwarzania w przeglądarce.
- Nieprawidłowy historyczny JSON, niezgodny typ, NaN, wykładnicza nieskończoność
  i niesparowany surogat dają jawne HTTP 409 zamiast awarii serializacji.
  Literalna zawartość bazy pozostaje niezmieniona i nadal przechodzi bezstratny
  obieg metadanych, jeżeli nie ma nieprawidłowego przypiętego selektora.
- Istniejący plik docelowy nie jest nadpisywany. Sprzeczne flagi zdolności,
  NaN jako liczba pakietu i przekroczenie limitu rekordów nie tworzą archiwum.
  Dodany widok SQL `jobs` lub zmieniony rekord archiwum blokuje eksport;
  docelowy plik JSON nie powstaje.

## Uwagi z przeglądu, które zmieniły implementację

1. Skan XLSX gubił oryginalny adres/typ komórki, a zły adres mógł otrzymać
   pozornie poprawny numer kolumny. `sourceReference` i `sourceType` zachowują
   pola źródła; błędny adres ma osobną diagnostykę, a zastępcza pozycja jest
   jawnie prowizoryczna.
2. Limit liczby plików nie ograniczał sumy długości ścieżek drzewa SAF.
   Wprowadzono wspólny budżet lokatorów, a retencja wyników uwzględnia również
   lokatory wierszy i adresy komórek. Liczba diagnostyk parsera jest ograniczona.
3. Sprawdzanie deklaracji DTD po usunięciu zer obejmowało popularne kodowania,
   lecz nie wszystkie kodowania respektowane przez SAX. Adapter jawnie
   obsługuje XML UTF-8, odrzuca inne deklaracje i DTD/entity oraz wyłącza
   zewnętrzne encje. UTF-16 XLSX nie jest obecnie obsługiwany przez ten adapter.
4. Błąd starego cache katalogu był ukrywany. Osobna obsługa błędów katalogu
   i cache skanu pokazuje przyczynę i pozwala kontynuować inicjalizację.
5. Zbyt szeroka walidacja zagnieżdżonego JSON mogła odrzucić poprawny cytat
   segmentowy z powodu nieużywanego błędnego pola słów. Selektor pozostaje
   ścisły; walidacja projekcji sprawdza używany zakres i zachowuje historyczne
   wartości poza nim. JSON całego pakietu nadal nie dopuszcza NaN jako liczby.
6. Zwrócenie nieprawidłowego historycznego transkryptu przez HTTP kończyło się
   błędem serializacji. Jawne 409 zachowuje źródło problemu bez modyfikacji danych.
7. Końcowa integracja koordynatora i N3 odrzuca powtórzone klucze JSON podczas
   tworzenia cytatu i udostępniania transkryptu przez HTTP. Zapobiega to
   niejednoznacznej interpretacji zapisanych pól; trzy dodatkowe testy weszły
   do końcowego przebiegu 180 testów i 207 podtestów.

## Granice wyniku

Nie wykonano rzeczywistej inferencji ASR, testu jakości transkrypcji ani testu
SAF/Compose na telefonie. Build i testy JVM potwierdza oddzielny zapis N5
[`ANDROID_NIGHT_BUILD_RECEIPT.json`](ANDROID_NIGHT_BUILD_RECEIPT.json);
przegląd kodu nie zastępuje tych dowodów. Testy Node używają atrapy audio.
Źródła syntetyczne użyte
przez QA nie zawierają prywatnego materiału użytkownika.

Natywny XLS pozostaje jawną nieobsługiwaną zdolnością; serwer ma osobny adapter.
XLSX to ograniczona projekcja zapisanych komórek, bez wykonywania formuł,
interpretacji stylów/dat lub ustalania tożsamości rozmówców. SAF może udostępniać
zmienny lub zdalny materiał; skan nie gwarantuje atomowego snapshotu drzewa.

Archiwum to dokładny obieg **metadanych**, bez mediów, podpisu, odtworzenia
żywej instalacji lub wznowienia jobs. Przedział cytatu słownego jest deklaracją
zapisanych znaczników ASR, nie pomiarem dopasowania do nagrania.

## Dodatkowy odbiór renderowania wyników

N4 odtworzył wykonanie HTML źródła przez FTS snippet i szablon z `safe`.
Dwa testy najpierw zawiodły dla script oraz img/onerror. Fragmenty są teraz
zwykłym tekstem, a szablon stosuje autoescape. Wyróżnienia FTS usunięto
z prezentacji; źródło i indeks zachowują dosłowną treść. `test_search_rendering.py`
sprawdza HTTP, API i niezmienione dane. Koordynator po tej zmianie wykonał
pełne 182 testy i 207 podtestów; nie było potrzeby ponawiania builda Android.
