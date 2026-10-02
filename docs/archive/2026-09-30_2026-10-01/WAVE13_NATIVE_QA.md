# Fala 13 — natywny kontrakt odpowiedzi zapisu cytatu

## Wynik

Rdzeń Kotlin zamraża teraz kanoniczny selektor wybranego wystąpienia, a
`CitationWorkspace.save()` odrzuca odpowiedź utworzenia przed resetem historii
i komunikatem sukcesu, jeżeli nie zgadzają się: dodatni ID, artefakt, wersja,
dokładny tekst, zakres milisekund albo selektor. Odczyt historycznych cytatów
nie został zaostrzony i nie wymaga migracji dawnych selektorów.

Opublikowane przyrosty:

- `32a38f76ada54a815d56b22bc34d126facbbcebd` — kontrakt rdzenia i testy;
- `f4827dd742da2b5812c4df54c2a264c4e76015dd` — ogrodzenie odpowiedzi w
  workspace Androida i testy skutków ubocznych.

## Odbiór wieloagentowy

Pięć plików testowych zawiera 29 przygotowanych przypadków właścicielskich i
niezależnych. Obejmują powtarzający się tekst oraz ten sam zaokrąglony zakres,
inny indeks/referencję, niedodatni ID, ścisły kształt selektora, kolejność
tablic, zamrożenie list wejściowych, spóźnione odpowiedzi i brak przedwczesnego
odświeżenia historii.

Niezależny przegląd wykrył w pierwszej wersji rzeczywisty kontrprzykład:
Python zapisuje najmniejszy dodatni `Double` jako `5e-324`, a JVM może użyć
`4.9E-324`. Poprawiony matcher jest zależny od roli pola: indeksy i referencje
porównuje jako dokładne liczby dziesiętne, natomiast `source_start` i
`source_end` jako tę samą skończoną wartość IEEE-754, którą przechowuje model
klienta. Osobna regresja zachowuje oba wymagania. Przegląd źródła po poprawce
uzyskał PASS.

## Rzeczywista walidacja i blokada

Uruchomiono:

```text
./gradlew --offline --no-daemon :core:desktopTest :androidApp:testDebugUnitTest
```

Wrapper próbował pobrać przypięty Gradle 9.7.0 i zakończył się przed
konfiguracją oraz kompilacją błędem `Network is unreachable`. W odnowionym
środowisku nie ma lokalnej dystrybucji Gradle, kompilatora Kotlin ani Android
SDK. Zatem **0 z 29 nowych testów wykonano**; nie powstał nowy APK i nie
zastąpiono ostatniego odebranego artefaktu. GitHub Actions nie uruchamiano.

## Jawne granice

- Serwer może zachować całkowite sekundy powyżej `2^53`, których model Androida
  `Double` nie reprezentuje dokładnie. Taka odpowiedź jest bezpiecznie
  odrzucana przez różny zakres milisekund; nie deklarujemy pełnej zgodności
  całego zakresu `Long`.
- Nie wykonano telefonu, SAF runtime, transportu HTTP, audio ani realnej
  inferencji ASR w tej fali.
- Nie powtarzano wcześniejszego odbioru web/Node i Chromium.
- PASS przeglądu źródła nie zastępuje kompilacji ani wykonania testów.

## Następny konkretny krok

W środowisku z cache Gradle 9.7.0, JDK 21 i Android SDK 37 uruchomić oba zadania
testowe oraz build debug. Dopiero po zielonym przebiegu odebrać N80/N81/N84,
zapisać receipt z liczbą wykonanych testów i ewentualnie zastąpić APK. Osobno,
gdy pojawi się Chromium i zależności HTTP, wykonać cztery przygotowane przypadki
N83; nie łączyć tego z dowodem natywnym.
