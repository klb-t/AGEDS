cmake_minimum_required(VERSION 3.22.1)
project(ageds_whisper LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(WHISPER_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(GGML_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(GGML_BUILD_TESTS OFF CACHE BOOL "" FORCE)
include(FetchContent)
FetchContent_Declare(
  whispercpp
  GIT_REPOSITORY https://github.com/ggml-org/whisper.cpp.git
  GIT_TAG d09f61a708f3487afa956ff578e60eae5e7a233c
  GIT_SHALLOW FALSE
)
FetchContent_MakeAvailable(whispercpp)
add_library(ageds_whisper SHARED whisper_jni.cpp)
target_include_directories(ageds_whisper PRIVATE ${whispercpp_SOURCE_DIR}/include)
find_library(log-lib log)
target_link_libraries(ageds_whisper PRIVATE whisper ${log-lib} android)

