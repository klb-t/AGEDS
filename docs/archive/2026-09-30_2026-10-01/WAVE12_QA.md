# Fala 12 — wierność projekcji tabel

Backend: **429 testów i 588 podtestów przeszło**, zero błędów, 3 istniejące
ostrzeżenia deprecation. Android/core: **379 testów JVM przeszło**,
57 zadań Gradle, 91 zgodnych hashy wejść i zweryfikowany APK/podpis.

Serwer 1.1.1: poprzedni splitlines rozdzielał m.in. U+2028, a csv.reader nie.
Skutkiem były błędne raw_record przypisane do poprawnie odczytanych wartości,
mimo complete=true. Parsowanie i capture korzystają teraz z tych samych
fizycznych CR/LF/CRLF bez tłumaczenia końców linii. Surowy rekord to oryginalny
zdekodowany tekst, nie oryginalne bajty z BOM; te identyfikuje osobny hash.
12 nowych testów obejmuje separatory Unicode/control, UTF16, multiline,
preamble, malformed fragment i granice wyników. Dwa rzeczywiste CLI scan
wykonały 28 asercji na 4 małych źródłach (711 B). Wszystkie hashe/mtime/bajty
źródeł pozostały identyczne, a manifesty powstały poza nimi.

Natywny XLSX: stary parser faktycznie zwrócił 東京とうきょう zamiast 東京,
łącząc tekst bazowy ze wskazówką wymowy rPh. Poprawka zachowuje tekst bazowy,
rich text, whitespace, indeks shared string i zapisany wynik formuły.
Pomijane wskazówki są jawne przez xlsx_phonetic_omitted i częściowe pokrycie;
nie twierdzimy, że zachowano pełny XML. Ich znaki nadal zużywają limit komórki.
22 nowe testy parsera oraz silnik→cache potwierdziły także brak zmian źródeł.
W pierwszym niezależnym control fixture brakowało referencji komórki;
naprawiono fixture, bez zmiany produkcji. Odbiór końcowy przeszedł.

Dowody: WAVE12_CSV_PHYSICAL_LINES.md, WAVE12_CSV_QA.md,
WAVE12_CSV_CLI_QA.md i RECEIPT.json, XLSX_PHONETIC_PROJECTION.md,
WAVE12_XLSX_QA.md, WAVE12_XLSX_ENGINE_QA.md, ANDROID_WAVE12_BUILD_RECEIPT.json.

Przeglądarka i ASR nie zmieniły się; nie deklarujemy ich ponownego wykonania.
Bez telefonu, prywatnego korpusu, Actions, płatnych usług ani nowych zależności.
Następny konkret: porównać odpowiedź zapisu cytatu z zamrożonym pełnym
selektorem i zakresem w obu klientach; identyczny tekst może wystąpić więcej
niż raz. Przeglądarka przyjmowała złą pozycję przy tym samym tekście i wersji;
nie wykazano błędnej produkcji takiego wyniku przez backend.
