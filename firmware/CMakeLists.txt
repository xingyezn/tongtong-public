# For more information about build system see
# https://docs.espressif.com/projects/esp-idf/en/latest/api-guides/build-system.html
# The following five lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

set(PROJECT_VER "2.1.0")

# Add this line to disable the specific warning
add_compile_options(-Wno-missing-field-initializers)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# 全局关闭 -Werror：第三方组件(GCC14)存在格式/声明历史告警,
# IDF 默认对组件强开 -Werror 导致编译失败
idf_build_set_property(COMPILE_OPTIONS "-Wno-error" APPEND)
idf_build_set_property(C_COMPILE_OPTIONS "-Wno-error" APPEND)

project(tongtong)

