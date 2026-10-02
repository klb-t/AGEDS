# Fala 13 — niezależny przegląd adwersarialny klienta natywnego

## Zakres

Przegląd obejmuje zamrożenie `SelectedCitation`, sprawdzenie odpowiedzi zapisu
w `CitationWorkspace.save()` oraz nowe testy kontraktu. Jest niezależny od
implementacji i nie zmienia kodu produkcyjnego ani testów. Sprawdzono konkretne
granice: mutowalność, liczby JSON, przepełnienia czasu, powtarzające się
wystąpienia, niedodatni identyfikator odpowiedzi, kolejność skutków ubocznych,
spóźnione odpowiedzi i zgodność odczytu historycznych cytatów.

## Odtworzona luka i stan integracji

Stara ścieżka zapisu porównywała jedynie artefakt, wersję, tekst i zakres po
zaokrągleniu. Dwa wystąpienia o identycznym tekście i tym samym zakresie mogły
więc być pomylone. Zintegrowana zmiana sprawdza dodatni zapisany ID i cały
kanoniczny selektor przed resetem historii, rozpoczęciem jej ponownego odczytu
oraz komunikatem sukcesu. Tablice indeksów i referencji zachowują kolejność;
zestawy kluczy obiektów są dokładne, ale kolejność kluczy nie ma znaczenia.

Listy wejściowe są kopiowane przy tworzeniu wyboru, a każde pobranie `request`
zwraca kolejne kopie. Zmiana wyboru unieważnia epoch zapisu. Poprawna, lecz
spóźniona odpowiedź poprzedniego wyboru nie resetuje historii, nie publikuje
komunikatu i nie zastępuje nowego podglądu. Walidacja odpowiedzi może wykonać
się przed sprawdzeniem nieaktualnego epoch, lecz jej błąd jest wtedy tłumiony i
nie powoduje skutku widocznego w bieżącym widoku.

Walidator działa wyłącznie na odpowiedzi operacji utworzenia. Odczyt istniejącej
historii nadal przyjmuje starsze, minimalne lub opaque selektory; nie powstała
ukryta migracja ani odrzucanie historycznych rekordów.

## Znaleziony kontrprzykład liczbowy

Pierwsza wersja matchera porównywała liczby jako dokładne wartości dziesiętne.
To zachowuje rozróżnienie dużych całkowitych, ale jest niepoprawne dla pól
`source_start` i `source_end`, które klient wcześniej dekoduje do `Double`.
Najmniejszą dodatnią wartość IEEE-754 Python serializuje jako `5e-324`, a JVM
17 jako `4.9E-324`. Oba napisy dekodują się do tego samego `Double` i dają ten
sam zakres 0 ms, lecz dokładna normalizacja dziesiętna uznawała je za różne.
Kontrprzykład odtworzono bez sieci poleceniami Python `json.dumps` i JVM
`Double.toString`. Został przekazany właścicielowi kontraktu przed odbiorem.

Poprawne rozstrzygnięcie musi być zależne od roli pola: indeksy segmentów oraz
referencje słów pozostają dokładnymi ograniczonymi liczbami całkowitymi, zaś
surowe granice czasu porównuje się jako skończone wartości reprezentowane przez
model `Double`. Napisy i wartości logiczne nie mogą być koercjonowane.

## Pozostała granica modelu

Serwer dopuszcza także całkowite sekundy większe niż `2^53`, o ile wynik mieści
się w zakresie milisekund SQLite. Model Androida przechowuje czas jako
`Double`, więc np. `9007199254740993` s dekoduje jako
`9007199254740992.0` s. Python oblicza dla pierwszej wartości
`9007199254740993000` ms, a klient dla drugiej inny zakres. Odpowiedź zostanie
bezpiecznie odrzucona, ale klient nie ma pełnej reprezentacyjnej zgodności z
całym zakresem liczbowym przyjmowanym przez serwer. To istniejąca granica
modelu, a nie przepełnienie w nowym matcherze. Jej pełne usunięcie wymagałoby
zachowania surowej liczby dziesiętnej albo zawężenia kontraktu czasu; nie należy
jej maskować twierdzeniem o pełnej zgodności `Long`.

## Werdykt i granice dowodu

Po poprawce path-aware niezależny przegląd źródła daje **PASS** dla kontraktu
i kolejności skutków ubocznych. Kontrprzykład Python–JVM ma osobną regresję,
a duże identyfikatory nadal przechodzą ścisłą ścieżką dziesiętną. Odbiór
wykonawczy pozostaje jednak zablokowany do przejścia zintegrowanych testów.
Przygotowane testy obejmują co najmniej:

- najmniejszą dodatnią wartość `Double` zapisaną dwiema reprezentacjami;
- ścisłość indeksów i kolejności tablic mimo łagodniejszego porównania czasu;
- identyczny tekst i zakres przy innych referencjach;
- niedodatni ID i zmienione metadane selektora;
- brak resetu historii i komunikatu po błędnej lub spóźnionej odpowiedzi;
- brak zmiany kontraktu odczytu historycznych cytatów.

Przegląd nie jest testem HTTP, urządzenia, odtwarzania, alignmentu ASR ani
poprawności backendu. W tym środowisku Gradle nie dotarł do kompilacji, więc
PASS oznacza wyłącznie niezależny odbiór źródła, nie wykonanie testów.
