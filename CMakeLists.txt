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
# scikit-build-core runs this file inside a build-isolation venv that has
# ``nanobind`` installed (per [build-system].requires in pyproject.toml),
# but doesn't automatically add nanobind's CMake config dir to
# CMAKE_PREFIX_PATH. Canonical workaround: ask the wheel where its
# CMake package lives via ``python -m nanobind --cmake_dir``.
find_package(Python 3.12 COMPONENTS Interpreter Development.Module REQUIRED)

execute_process(
    COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    OUTPUT_VARIABLE NB_DIR
    RESULT_VARIABLE NB_DIR_RESULT
)
if(NOT NB_DIR_RESULT EQUAL 0)
    message(FATAL_ERROR
        "Could not locate nanobind's CMake package via "
        "'${Python_EXECUTABLE} -m nanobind --cmake_dir'. Is nanobind "
        "installed in this Python environment?")
endif()
list(APPEND CMAKE_PREFIX_PATH "${NB_DIR}")
find_package(nanobind CONFIG REQUIRED)

nanobind_add_module(
    _native
    STABLE_ABI
    src/_aiolibdatachannel/bindings.cpp
)

# Force the ``abi3`` suffix on Linux/macOS. ``STABLE_ABI`` already
# compiles against ``Py_LIMITED_API``, but nanobind/CMake still emits
# the per-CPython-version filename (``_native.cpython-312-…so``)
# unless we override here. With that filename, Python's importer
# only finds the module on the exact build-time CPython (3.12);
# 3.13+ users see ``ImportError: cannot import name '_native'``.
# Renaming to ``_native.abi3.so`` makes the same compiled module
# loadable by every Python ≥ 3.12 — which is the whole point of
# the stable ABI build.
#
# Windows uses ``.pyd`` for both abi3 and per-version modules
# (no separate marker), so leave the default suffix there.
if(NOT WIN32)
    set_target_properties(_native PROPERTIES SUFFIX ".abi3.so")
endif()

# The nanobind module calls libdatachannel's C API (rtc/rtc.h). Linking
# the static ``datachannel`` target drags in libdatachannel + static deps.
target_link_libraries(_native PRIVATE datachannel)
target_include_directories(_native PRIVATE vendor/libdatachannel/include)

# Place the built extension at aiolibdatachannel/_native.<abi>.so inside
# the wheel so ``from aiolibdatachannel import _native`` resolves without
# any sys.path gymnastics.
install(TARGETS _native LIBRARY DESTINATION aiolibdatachannel)
