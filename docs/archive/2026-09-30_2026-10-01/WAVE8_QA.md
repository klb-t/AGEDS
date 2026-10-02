# Odbiór fali 8

Serwerowy skaner 1.1.0 odróżnia deklaracje nagłówka WAV od rzeczywistego
rozmiaru pliku. Potwierdzony wcześniejszy błąd: 44-bajtowy plik deklarujący
16000 bajtów danych dostawał czas 1 s i kompletny skan bez ostrzeżeń.
Teraz zachowuje surowe wartości, zgłasza sprzeczność i nie emituje dostępnego
czasu. Pełny hash rzeczywistych 44 bajtów jest odrębną obserwacją.

Nagłówki mają limit 64 KiB/plik i domyślnie 8 MiB razem. Każdy zwrócony bajt
jest rozliczony, także przed późniejszym błędem. Hashe i tabele zachowują swoje
odrębne budżety. Nie twierdzimy, że liczniki opisują fizyczny transfer dysku
lub sieci. Nie zmieniano ani nie kopiowano korpusu.

Niezależne próby ujawniły i potwierdziły poprawki trzech dodatkowych błędów:
nieaktualnego licznika zaakceptowanych hashy po mutacji, braku wykrywania zmiany
tego samego pliku między enumeracją a otwarciem oraz wycieku deskryptora przy
błędzie fdopen. Błąd końcowego stat/close także unieważnia hash i wyliczony czas;
surowe deklaracje oraz pierwotna obserwacja pozostają z jawnym statusem błędu.

Pełny backend: **370 testów i 506 podtestów**, zero błędów, trzy istniejące
ostrzeżenia deprecacji. Rzeczywisty subprocess CLI skanował pięć syntetycznych
WAV: prawidłowy, ucięty, duży, tysiące pustych bloków i nieobsługiwany format.
Manifest trafił poza źródła; nazwy, bajty, rozmiary i mtime nie zmieniły się.
Nie powstał cache ani kopia audio. Receipty zawierają zgodne hashe kodu.

Oba rzeczywiste parsery — skompilowany Kotlin i Python — otrzymały identyczne
bajty w 24 przypadkach. Przeszło 408 dokładnych porównań uzgodnionych pól.
To zgodność tych projekcji dla wspólnego zakresu parametrów, nie dowód
równoważności SAF i POSIX, wyczerpująca obsługa WAV ani walidacja próbek.

Natywne źródła aplikacji nie zmieniły się: ostatni build pozostaje z fali 7,
287 JVM i APK `284203bf79260c8266f16c7515b6b72609152723c17523e69109dfb6be7ece1d`.
Nie uruchamiano ponownie przeglądarki ani ASR, których zakres ten przyrost nie
zmienia. Nie wykonano telefonu/SAF runtime ani integracji innego projektu.

Dowody: `SERVER_WAV_HEADER.md`, `SERVER_WAV_HEADER_SCAN.md`,
`WAVE8_WAV_QA.md`, `WAVE8_STREAM_QA.md`, `WAVE8_WAV_PARITY_RECEIPT.json`,
`SERVER_WAV_SCAN_SMOKE_RECEIPT.json`.

Następna luka odbioru: natywna polityka przechodzenia całego drzewa źródeł
pozostaje powiązana z Android Context. Parsery i czytniki wykonano na JVM,
ale brak telefonu blokuje sprawdzenie ich wspólnej orkiestracji przez SAF.
Warto wydzielić wąski interfejs dostawcy tylko do odczytu i sprawdzić tę samą
produkcyjną politykę na syntetycznym dostawcy hosta, nadal nie nazywając tego
testem prawdziwego SAF ani zastępstwem odbioru urządzenia.
