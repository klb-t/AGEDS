# Android — odbiór fali 12

Pierwszy izolowany build przeszedł: 57 zadań Gradle, 268 testów Android JVM
oraz 111 desktop JVM, razem 379. Zero błędów/pominięć. 91 hashy wejść
zweryfikowano wobec źródeł; źródła niezmienione, podpis APK z kodem 0.
SHA-256 APK: `ab1ab5453fba73255cc8779cf342d1059e9bb7af2cd9e035a9ae7e0b693655fb`.

22 nowe testy: 7 autora, 12 niezależnych przypadków XLSX i 3 rzeczywistego
silnika/cache. Pierwotna korupcja tekstu została wykonana przed poprawką.
Pełny Gradle obejmuje dokładnie poprawione źródła i testy. Nie wykonano
telefonu/SAF runtime; sześć testów instrumentation z fali 11 pozostaje tylko
skompilowanym dowodem. Nie powtarzano ich kompilacji dla prywatnej zmiany
parsera bez zmiany interfejsu. Receipt: ANDROID_WAVE12_BUILD_RECEIPT.json.
