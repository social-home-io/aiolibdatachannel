cmake_minimum_required(VERSION 3.24)
project(aiolibdatachannel LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# --- libdatachannel configuration -------------------------------------------
# Build libdatachannel as a shared library that cffi loads at runtime.
# Dependencies (usrsctp, libjuice, openssl) link statically into it so the
# wheel ships a single self-contained `.so` / `.dylib` / `.dll`.
set(BUILD_SHARED_LIBS ON CACHE BOOL "" FORCE)
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

# --- Ship the shared library inside the Python package ----------------------
# The LibDataChannel shared target already encodes the correct versioned
# soname. scikit-build-core will place the installed files under the package
# directory in the built wheel.
install(TARGETS datachannel
    LIBRARY DESTINATION aiolibdatachannel/_lib
    RUNTIME DESTINATION aiolibdatachannel/_lib)

# Set rpath so the shared lib can find its transitive deps (none in our
# static-deps layout, but future-proofing in case we ever flip one to shared).
if(APPLE)
    set_target_properties(datachannel PROPERTIES
        INSTALL_RPATH "@loader_path")
elseif(UNIX)
    set_target_properties(datachannel PROPERTIES
        INSTALL_RPATH "$ORIGIN")
endif()
