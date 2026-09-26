plugins {
    id("org.jetbrains.kotlin.multiplatform")
    id("com.android.kotlin.multiplatform.library")
    id("org.jetbrains.kotlin.plugin.serialization")
}

kotlin {
    androidLibrary {
        namespace = "dev.klbt.ageds.core"
        compileSdk = 36
        minSdk = 26
    }
    jvm("desktop")
    js(IR) { browser(); nodejs() }
    wasmJs { browser() }

    sourceSets {
        commonMain.dependencies {
            implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
        }
        commonTest.dependencies { implementation(kotlin("test")) }
    }
}
