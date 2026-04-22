cmake_minimum_required(VERSION 3.24)
project(aiolibdatachannel LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# --- libdatachannel configuration -------------------------------------------
# Build libdatachannel as a static library linked directly into the nanobind
# extension. Dependencies (usrsctp, libjuice, openssl) link statically as
# well so the wheel ships one self-contained extension module.
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(NO_EXAMPLES ON CACHE BOOL "" FORCE)
set(NO_TESTS ON CACHE BOOL "" FORCE)
set(NO_WEBSOCKET ON CACHE BOOL "" FORCE)
set(NO_MEDIA ON CACHE BOOL "" FORCE)
set(PREFER_SYSTEM_LIB OFF CACHE BOOL "" FORCE)
set(USE_NICE OFF CACHE BOOL "" FORCE)

# Default TLS backend: OpenSSL. Override with -DUSE_MBEDTLS=ON or -DUSE_GNUTLS=ON.
if(NOT DEFINED USE_MBEDTLS AND NOT DEFINED USE_GNUTLS)
    set(USE_MBEDTLS OFF CACHE BOOL "" FORCE)
    set(USE_GNUTLS OFF CACHE BOOL "" FORCE)
endif()

add_subdirectory(vendor/libdatachannel)

# --- nanobind extension -----------------------------------------------------
# Locate the nanobind CMake package that ships with the pip-installed
# ``nanobind`` wheel. scikit-build-core puts it on the CMAKE_PREFIX_PATH.
find_package(Python 3.12 COMPONENTS Interpreter Development.Module REQUIRED)
find_package(nanobind CONFIG REQUIRED)

nanobind_add_module(
    _native
    STABLE_ABI
    src/_aiolibdatachannel/bindings.cpp
)

# The nanobind module calls libdatachannel's C API (rtc/rtc.h). Linking
# the static ``datachannel`` target drags in libdatachannel + static deps.
target_link_libraries(_native PRIVATE datachannel)
target_include_directories(_native PRIVATE vendor/libdatachannel/include)

# Place the built extension at aiolibdatachannel/_native.<abi>.so inside
# the wheel so ``from aiolibdatachannel import _native`` resolves without
# any sys.path gymnastics.
install(TARGETS _native LIBRARY DESTINATION aiolibdatachannel)
