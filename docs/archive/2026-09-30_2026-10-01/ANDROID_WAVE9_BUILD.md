# Android — odbiór fali 9

Izolowany assembleDebug, testDebugUnitTest i desktopTest przeszedł: 57 zadań,
226 testów Android JVM i 111 desktop JVM, razem 337. Zero błędów/pominięć.
Sprawdzono 82 hashe wejść, niezmienność źródeł i podpis APK (kod 0).
SHA-256 APK: `05d4eea3b424b79f21fdf89b2687da7992ee297bb39c160ed9836c61e80044bc`.

Pierwszy build przerwał się przy kompilacji testu z importami kotlin.test,
nieobecnymi w konfiguracji Androida. Ręczny kompilator miał dodatkowy classpath.
Poprawiono importy na istniejący JUnit bez nowych zależności. Oba receipty
zachowano: ANDROID_WAVE9_INITIAL_BUILD_RECEIPT.json i ANDROID_WAVE9_BUILD_RECEIPT.json.

Wykonano rzeczywisty produkcyjny silnik przez syntetycznego dostawcę JVM;
nie wykonano runtime SAF, telefonu ani prywatnego korpusu. Istniejących sześciu
testów instrumentation nie uruchomiono ani ponownie nie kompilowano w tej fali.
Backend/browser/ASR nie zmieniły się i nie zostały ponownie uruchomione.
