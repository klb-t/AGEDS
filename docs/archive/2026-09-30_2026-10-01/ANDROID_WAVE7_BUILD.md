# Android — odbiór fali 7

Izolowany assembleDebug, testDebugUnitTest i desktopTest przeszedł: 57 zadań
Gradle, 176 testów Android JVM i 111 desktop JVM, razem 287. Bez błędów
i pominięć. Sprawdzono 76 hashy wejść i niezmienność źródeł w czasie buildu.
Podpis APK zweryfikowano z kodem 0.

SHA-256 APK: `284203bf79260c8266f16c7515b6b72609152723c17523e69109dfb6be7ece1d`.

Wykonano nowe testy nagłówka WAV, strumienia, prezentacji i zgodności cache,
wraz z wcześniejszymi regresjami. Źródła testowe są syntetyczne. Nie wykonano
runtime SAF, telefonu, odsłuchu ani testu prywatnego korpusu. Kompilacja
skanera potwierdza połączenie typów; zachowanie dostawcy Androida wymaga
odrębnego odbioru. Dokładny zapis: `ANDROID_WAVE7_BUILD_RECEIPT.json`.
