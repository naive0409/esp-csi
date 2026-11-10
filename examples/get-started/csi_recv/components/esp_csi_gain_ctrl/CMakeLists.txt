idf_component_register(SRCS ""
                       INCLUDE_DIRS "include")

add_prebuilt_library(esp_csi_gain_ctrl_lib "${CMAKE_CURRENT_SOURCE_DIR}/6.0/${CONFIG_IDF_TARGET}/libesp_csi_gain_ctrl.a")
target_include_directories(${COMPONENT_LIB} INTERFACE "${CMAKE_CURRENT_SOURCE_DIR}/include")
target_link_libraries(${COMPONENT_LIB} INTERFACE esp_csi_gain_ctrl_lib)
