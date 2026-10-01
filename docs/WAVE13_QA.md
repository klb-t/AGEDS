# Fala 13 — zgodność odpowiedzi zapisu cytatu w kliencie webowym

Kod opublikowany: `346425bd66611505ce52b20bca01e5779e73d620`.
Przejęto wygasły claim po porównaniu zdalnego HEAD; od poprzedniego claimu
nie było nowych commitów. Nowa rezerwacja została opublikowana przed implementacją.
Czterech rozłącznych agentów prowadziło implementację, kontrakt, testy obsługi
interfejsu i niezależny przegląd; koordynator integrował i publikował.

## Wynik

Klient zamraża wybrany tekst, wersję, uporządkowany selektor i zakres przed POST.
Odpowiedź musi dokładnie odpowiadać tej projekcji, zanim pokaże potwierdzenie
lub doda cytat do historii, przycisk odsłuchu i eksport. Identyczny tekst oraz
czas nie wystarczają: inne wystąpienie słów lub segmentów jest odrzucane.
Metadane selektora zachowują typy; kolejność kluczy obiektu nie ma znaczenia,
a liczby JSON `0` i `0.0` są równoważne. Tekst nie jest normalizowany.

Milisekundy używają zaokrąglania ties-to-even po mnożeniu binarnej liczby przez
1000, zgodnego z Pythonem. Klient jawnie odmawia liczb spoza dokładnego zakresu
całkowitego JavaScript. Nie rozszerzono kontraktu odczytu historycznych cytatów.
Spóźniona poprawna odpowiedź trafia do historii tego samego artefaktu, zachowując
starą wersję; nie zastępuje statusu nowo wybranej wersji. Błędna odpowiedź nie
trafia do historii również po zmianie wersji.

## Wykonany odbiór

`node --test server/tests/js/*.test.mjs`: **85 testów, 0 błędów, 0 pominiętych**.
W tym 49 wcześniejszych, 4 właściciela, 23 niezależne kontraktu, 6 obsługi zapisu
z symulowanym DOM oraz 3 porównujące rzeczywiste funkcje Pythona i JavaScript.
Te ostatnie obejmują **3008 przypadków numerycznych i dwie projekcje**.
Wymagają dostępnego `python3` lub ustawienia `PYTHON`; bez zewnętrznych bibliotek.

Niezależny test tych samych sześciu scenariuszy obsługi interfejsu na poprzednim
kodzie dał 4 błędy i 2 sukcesy; na poprawionym kodzie wszystkie sześć przeszło.
Przegląd niezależny: `WAVE13_SAVE_REVIEW.md`. Szczegółowe kontrakty i granice:
`WAVE13_BROWSER_CONTRACT_QA.md`, `WAVE13_BROWSER_RUNTIME_QA.md`.
Hashe wejść i zbiorczy wynik: `WAVE13_BROWSER_RECEIPT.json`.

Przygotowano cztery dodatkowe przypadki Chromium; sprawdzono składnię skryptu.
Wykonano inicjalizację rzeczywistej syntetycznej bazy fixture, włącznie ze 125
wersjami historii i sześcioma dużymi cytatami. Tylko `uvicorn.run` zastąpiono
funkcją zatrzymującą się przed serwerem; wykonała ona produkcyjny zapis pierwszego
słowa powtórzonego tekstu i potwierdziła zakres 0–2 ms oraz właściwy selektor.
Nie jest to test HTTP ani Chromium.

## Granice i następny krok

Obecne środowisko nie zachowało wcześniejszego scratch, SDK Androida, cache
Gradle ani Chromium. Domyślny i główny Python nie mają FastAPI/uvicorn.
Rzeczywisty start Chromium odmówił z powodu braku pliku wykonywalnego.
Nie zmieniono APK ani kodu Kotlin; N80/N81/N84 pozostają gotowe do podjęcia.
N83 ma wykonany odbiór symulowanego DOM, lecz odbiór Chromium pozostaje otwarty.
Historyczne wyniki backendu, JVM, Chromium i ASR nie są wynikami tej fali.

Następny konkretny przyrost: odpowiednik zamrożonego selektora w
`CitationSelection` i walidacji w `CitationWorkspace.save`, z testami JVM po
odzyskaniu toolchainu. Dodatkowo uruchomić przygotowany skrypt Chromium
z nową ścieżką `AGEDS_BROWSER_RECEIPT`, zachowując poprzednie receipty.
Spójność odpowiedzi nie uwierzytelnia serwera, nie dowodzi prawdy ASR ani
alignmentu. Nie wykonano testu telefonu, nowej inferencji ani integracji partnera.
Nie uruchamiano Actions, płatnych usług ani modeli. Korpus pozostaje nietknięty.
