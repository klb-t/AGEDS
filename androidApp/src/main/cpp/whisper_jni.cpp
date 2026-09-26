#include <jni.h>
#include <string>
#include <sstream>
#include <algorithm>
#include <thread>
#include "whisper.h"

extern "C" JNIEXPORT jlong JNICALL
Java_dev_klbt_ageds_whisper_WhisperNative_init(JNIEnv *env, jobject, jstring path) {
    const char *p = env->GetStringUTFChars(path, nullptr);
    whisper_context_params cp = whisper_context_default_params();
    whisper_context *ctx = whisper_init_from_file_with_params(p, cp);
    env->ReleaseStringUTFChars(path, p);
    return reinterpret_cast<jlong>(ctx);
}

extern "C" JNIEXPORT jstring JNICALL
Java_dev_klbt_ageds_whisper_WhisperNative_transcribe(JNIEnv *env, jobject, jlong ptr, jfloatArray audio) {
    auto *ctx = reinterpret_cast<whisper_context *>(ptr);
    if (!ctx) {
        jclass ex = env->FindClass("java/lang/IllegalStateException");
        env->ThrowNew(ex, "Whisper context is null");
        return nullptr;
    }
    const jsize n = env->GetArrayLength(audio);
    jfloat *samples = env->GetFloatArrayElements(audio, nullptr);
    whisper_full_params params = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
    params.print_realtime = false;
    params.print_progress = false;
    params.print_timestamps = false;
    params.print_special = false;
    params.translate = false;
    params.language = "auto";
    params.no_context = true;
    const unsigned hc = std::max(2u, std::thread::hardware_concurrency());
    params.n_threads = static_cast<int>(std::min(6u, hc));
    const int rc = whisper_full(ctx, params, samples, n);
    env->ReleaseFloatArrayElements(audio, samples, JNI_ABORT);
    if (rc != 0) {
        jclass ex = env->FindClass("java/lang/RuntimeException");
        env->ThrowNew(ex, "whisper_full failed");
        return nullptr;
    }
    std::ostringstream out;
    const int count = whisper_full_n_segments(ctx);
    for (int i = 0; i < count; ++i) {
        const int64_t t0 = whisper_full_get_segment_t0(ctx, i) * 10;
        const int64_t t1 = whisper_full_get_segment_t1(ctx, i) * 10;
        out << "[" << t0 << "-" << t1 << " ms] " << whisper_full_get_segment_text(ctx, i) << "\n";
    }
    const std::string text = out.str();
    return env->NewStringUTF(text.c_str());
}

extern "C" JNIEXPORT void JNICALL
Java_dev_klbt_ageds_whisper_WhisperNative_free(JNIEnv *, jobject, jlong ptr) {
    auto *ctx = reinterpret_cast<whisper_context *>(ptr);
    if (ctx) whisper_free(ctx);
}
