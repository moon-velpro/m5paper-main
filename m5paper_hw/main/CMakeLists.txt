idf_component_register(
    SRCS "main.cpp"
    INCLUDE_DIRS "."
    REQUIRES esp_http_server esp_http_client nvs_flash esp_wifi esp_event esp_netif esp_timer esp_system esp_hw_support json mbedtls
)
