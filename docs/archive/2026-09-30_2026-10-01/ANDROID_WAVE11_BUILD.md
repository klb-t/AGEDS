# Android — odbiór fali 11

Pierwszy izolowany build przeszedł: 57 zadań Gradle, 246 testów Android JVM
oraz 111 desktop JVM, razem 357. Zero błędów i pominięć. 88 hashy wejść
zgodnych z repo, źródła niezmienione podczas buildu, podpis APK z kodem 0.
SHA-256 APK: `972a1f99b212dc58febe98b33ca7ced47ce3cbe02ac3644d0392aef246e63073`.

20 nowych testów obejmuje bramkę generacji, oba porządki wyścigu, odrzucenie
spóźnionej publikacji, rzeczywisty przepływ silnik → cache → typed readback
oraz odzyskanie po błędzie przeniesienia pliku. Testy używają barier i
syntetycznych źródeł, bez uzależnienia poprawności od losowego opóźnienia.

ViewModel i wrapper Androida skompilowano oraz niezależnie przejrzano.
Nie wykonano runtime Android/SAF, telefonu, prywatnego korpusu ani odsłuchu.
Backend/browser/ASR nie zmieniły się w tej fali; wcześniejsze odbiory nie są
nowym wykonaniem. Pełny receipt: ANDROID_WAVE11_BUILD_RECEIPT.json.

Dodatkowo ponownie skompilowano sześć testów instrumentation z aktualnym
kodem: assembleDebugAndroidTest offline, 26 zadań wykonanych i 34 aktualne.
Pierwsza próba odmówiła pobierania brakujących zależności; powtórka użyła
już istniejącego cache dostawcy. Zero pobrań. SyntheticDocumentsProvider nie
trafił do manifestu aplikacji debug. Runtime testów nadal **0**. Dowód:
`ANDROID_WAVE11_SAF_COMPILE_RECEIPT.json`.
