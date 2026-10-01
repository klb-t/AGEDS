# Odbiór fali 7

Natywny skaner może odczytać metadane dużego WAV bez kopiowania nagrania.
Zachowuje co najwyżej 64 KiB początku pliku, a znany duży plik odczytuje
tylko do tej granicy, dodatkowo ograniczonej budżetem pliku i całego skanu.
Przy nieznanym rozmiarze oblicza hash strumieniowo w granicach budżetu,
bez zachowywania całego audio w pamięci. Każdy odczytany bajt jest rozliczony
także po błędzie; anulowanie zamyka strumień i przerywa skan.

Czas pochodzi z deklarowanego rozmiaru danych i szybkości bajtowej nagłówka.
Nie jest pomiarem odtwarzania. Sprzeczny rozmiar źródła albo rzeczywistego EOF,
uszkodzona geometria, nieobsługiwany format i nieodczytane nagłówki pozostają
jawne. Całkowity hash i inspekcja nagłówka mają odrębne zakresy. Dokładna
granica bajtów bez zaobserwowanego EOF nie daje pełnego hasha.

Interfejs pokazuje pochodzenie czasu, zakres odczytu i brak sprawdzenia
próbek. Brak nowego pola nie staje się fikcyjną historią wcześniejszego skanu.
Starszy cache nadal się odczytuje; nowy zapis zawiera ograniczone metadane,
bez surowego lub zakodowanego audio. Źródła pozostają bez zmian.

Pełny build: **287 JVM** (176 Android, 111 desktop), 57 zadań Gradle,
76 hashy wejść, poprawny podpis APK. Bez błędów i pominięć. Bezpośrednie
niezależne próby obejmowały parser, strumień, UI i cache; ich nakładających się
liczb nie należy dodawać do wyniku buildu. Backend 321+430 podtestów,
49 Node, 21 Chromium i rzeczywisty ASR pozostają odbiorem niezmienionej
części serwerowej z fali 6; nie deklarujemy ich ponownego uruchomienia tutaj.

Nie wykonano telefonu, SAF runtime ani dekodowania audio w tym przyroście.
Nie zmieniano źródeł korpusu. Stary nienatywny skaner serwera ma odrębną
politykę i nie otrzymuje automatycznie gwarancji tego parsera.

Dowody: `WAV_HEADER_PROBE.md`, `WAVE7_WAV_QA.md`, `WAVE7_STREAM_QA.md`,
`WAVE7_CACHE_QA.md`, `ANDROID_WAV_HEADER_UI.md`,
`ANDROID_WAVE7_BUILD_RECEIPT.json`.

Następny krok wynika z reprodukcji: serwerowy skaner uznał 44-bajtowy WAV
z deklaracją 16000 bajtów danych za sekundowe nagranie i kompletny skan,
mimo braku próbek. Odczyt nagłówków serwera wymaga ograniczenia oraz jawnych
sprzeczności rozmiaru. To następny zakres, nie naprawa zadeklarowana tym
checkpointem.
