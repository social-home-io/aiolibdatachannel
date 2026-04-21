cmake_minimum_required(VERSION 3.24)
project(aiolibdatachannel LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# --- Python + nanobind ------------------------------------------------------
if(NOT DEFINED Python_EXECUTABLE AND DEFINED PYTHON_EXECUTABLE)
    set(Python_EXECUTABLE "${PYTHON_EXECUTABLE}")
endif()

find_package(Python 3.12 REQUIRED
    COMPONENTS Interpreter Development.Module Development.SABIModule)

execute_process(
    COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
    OUTPUT_STRIP_TRAILING_WHITESPACE
    OUTPUT_VARIABLE NB_CMAKE_DIR
)
list(APPEND CMAKE_PREFIX_PATH "${NB_CMAKE_DIR}")
find_package(nanobind CONFIG REQUIRED)

# --- libdatachannel configuration -------------------------------------------
# Set options BEFORE add_subdirectory so libdatachannel picks them up.
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

add_subdirectory(vendor/libdatachannel EXCLUDE_FROM_ALL)

# --- Python extension module ------------------------------------------------
nanobind_add_module(
    _aiolibdatachannel
    STABLE_ABI
    NB_STATIC
    src/_aiolibdatachannel/bindings.cpp
)

target_link_libraries(_aiolibdatachannel PRIVATE LibDataChannel::LibDataChannelStatic)

target_compile_definitions(_aiolibdatachannel PRIVATE RTC_STATIC)

if(MSVC)
    target_compile_options(_aiolibdatachannel PRIVATE /W3 /permissive-)
else()
    target_compile_options(_aiolibdatachannel PRIVATE -Wall -Wextra -Wno-unused-parameter)
endif()

install(TARGETS _aiolibdatachannel LIBRARY DESTINATION aiolibdatachannel)
install(FILES src/aiolibdatachannel/_aiolibdatachannel.pyi
        DESTINATION aiolibdatachannel)
