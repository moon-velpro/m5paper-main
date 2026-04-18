/**
 * M5PaperS3 中文看板完整版
 *
 * 1. 未配置时自动开启热点 M5Paper-Setup，并在 192.168.4.1 提供本地配置页
 * 2. 已配置时连接家庭 Wi‑Fi，请求后端 /dashboard JSON
 * 3. 渲染中文天气/路线信息到墨水屏后进入深睡眠
 *
 * 说明：
 * - 仍然需要从 secrets.h.example 复制一份 secrets.h，作为默认兜底配置
 * - 地址、天气、路线起终点会作为 query 参数追加到 API_URL，后端可直接读取覆盖
 */

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include <M5Unified.h>
#include "cJSON.h"
#include "clothing_sprites.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "weather_icons_2bit.h"
#include "esp_log.h"
#include "esp_system.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets_defaults.h"
#endif

#define SCREEN_W            960
#define SCREEN_H            540
#define MAX_HTTP_BUF        24576
#define CONFIG_NAMESPACE    "dashboard"
#define AP_SSID_DEFAULT     "M5Paper-Setup"
#define AP_PASS_DEFAULT     "12345678"
#define WIFI_CONNECTED_BIT  BIT0
#define WIFI_FAIL_BIT       BIT1

static EventGroupHandle_t s_wifi_events;
static int s_retry_count = 0;
static bool s_network_ready = false;
static bool s_wifi_ready = false;
static httpd_handle_t s_httpd = nullptr;
static bool s_config_saved = false;
static const char *TAG = "m5paper";

struct DeviceConfig {
    std::string wifi_ssid;
    std::string wifi_pass;
    std::string api_url;
    std::string home_address;
    std::string weather_address;
    std::string transit_from;
    std::string transit_to;
    int refresh_minutes = 15;
};

static DeviceConfig g_cfg;
static char *http_buf = nullptr;
static int http_buf_len = 0;

static const char *fallback_if_placeholder(const char *value) {
    if (!value || !*value) return "";
    if (strstr(value, "your-") == value || strstr(value, "https://your-") == value) return "";
    return value;
}

static inline uint32_t gray(int v) {
    return M5.Display.color888(v, v, v);
}

static void drawTemp(int x, int y, const char *num_str, uint32_t color, int circle_r = 4) {
    auto &d = M5.Display;
    d.setTextColor(color);
    int w = d.drawString(num_str, x, y);
    int cx = x + w + circle_r + 2;
    int cy = y + circle_r + 4;
    d.drawCircle(cx, cy, circle_r, color);
}

static std::string utf8_truncate(const char *input, size_t max_chars) {
    if (!input) return "";
    std::string out;
    size_t chars = 0;
    const unsigned char *p = reinterpret_cast<const unsigned char *>(input);
    while (*p && chars < max_chars) {
        size_t len = 1;
        if ((*p & 0x80) == 0x00) len = 1;
        else if ((*p & 0xE0) == 0xC0) len = 2;
        else if ((*p & 0xF0) == 0xE0) len = 3;
        else if ((*p & 0xF8) == 0xF0) len = 4;
        out.append(reinterpret_cast<const char *>(p), len);
        p += len;
        chars++;
    }
    if (*p) out += "…";
    return out;
}

static std::string utf8_slice(const char *input, size_t start_char, size_t max_chars) {
    if (!input || max_chars == 0) return "";

    std::string out;
    size_t chars = 0;
    const unsigned char *p = reinterpret_cast<const unsigned char *>(input);
    while (*p && chars < start_char) {
        size_t len = 1;
        if ((*p & 0x80) == 0x00) len = 1;
        else if ((*p & 0xE0) == 0xC0) len = 2;
        else if ((*p & 0xF0) == 0xE0) len = 3;
        else if ((*p & 0xF8) == 0xF0) len = 4;
        p += len;
        chars++;
    }

    chars = 0;
    while (*p && chars < max_chars) {
        size_t len = 1;
        if ((*p & 0x80) == 0x00) len = 1;
        else if ((*p & 0xE0) == 0xC0) len = 2;
        else if ((*p & 0xF0) == 0xE0) len = 3;
        else if ((*p & 0xF8) == 0xF0) len = 4;
        out.append(reinterpret_cast<const char *>(p), len);
        p += len;
        chars++;
    }
    return out;
}

static std::string url_encode(const std::string &value) {
    static const char *hex = "0123456789ABCDEF";
    std::string out;
    for (unsigned char c : value) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~') {
            out.push_back(c);
        } else if (c == ' ') {
            out.push_back('+');
        } else {
            out.push_back('%');
            out.push_back(hex[(c >> 4) & 0x0F]);
            out.push_back(hex[c & 0x0F]);
        }
    }
    return out;
}

static std::string url_decode(const std::string &value) {
    std::string out;
    for (size_t i = 0; i < value.size(); ++i) {
        if (value[i] == '+') {
            out.push_back(' ');
        } else if (value[i] == '%' && i + 2 < value.size()) {
            char h1 = value[i + 1];
            char h2 = value[i + 2];
            auto hex_val = [](char c) -> int {
                if (c >= '0' && c <= '9') return c - '0';
                if (c >= 'a' && c <= 'f') return c - 'a' + 10;
                if (c >= 'A' && c <= 'F') return c - 'A' + 10;
                return 0;
            };
            out.push_back(static_cast<char>((hex_val(h1) << 4) | hex_val(h2)));
            i += 2;
        } else {
            out.push_back(value[i]);
        }
    }
    return out;
}

static std::map<std::string, std::string> parse_form_encoded(const std::string &body) {
    std::map<std::string, std::string> params;
    size_t start = 0;
    while (start < body.size()) {
        size_t end = body.find('&', start);
        if (end == std::string::npos) end = body.size();
        size_t eq = body.find('=', start);
        if (eq != std::string::npos && eq < end) {
            std::string key = url_decode(body.substr(start, eq - start));
            std::string val = url_decode(body.substr(eq + 1, end - eq - 1));
            params[key] = val;
        }
        start = end + 1;
    }
    return params;
}

static esp_err_t load_string_pref(nvs_handle_t nvs, const char *key, std::string &out) {
    size_t len = 0;
    esp_err_t err = nvs_get_str(nvs, key, nullptr, &len);
    if (err != ESP_OK || len == 0) return err;
    std::vector<char> buf(len);
    err = nvs_get_str(nvs, key, buf.data(), &len);
    if (err == ESP_OK) out.assign(buf.data());
    return err;
}

static void save_string_pref(nvs_handle_t nvs, const char *key, const std::string &value) {
    nvs_set_str(nvs, key, value.c_str());
}

static void load_string_pref_compat(nvs_handle_t nvs, const char *primary_key, const char *legacy_key, std::string &out) {
    if (load_string_pref(nvs, primary_key, out) != ESP_OK && legacy_key) {
        load_string_pref(nvs, legacy_key, out);
    }
}

static int clamp_refresh_minutes(int value) {
    if (value < 5) return 5;
    if (value > 240) return 240;
    return value;
}

static void load_device_config(DeviceConfig &cfg) {
    cfg.wifi_ssid = fallback_if_placeholder(WIFI_SSID);
    cfg.wifi_pass = fallback_if_placeholder(WIFI_PASS);
    cfg.api_url = fallback_if_placeholder(API_URL);
    cfg.refresh_minutes = 15;

    nvs_handle_t nvs;
    if (nvs_open(CONFIG_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        load_string_pref_compat(nvs, "wifi_ssid", nullptr, cfg.wifi_ssid);
        load_string_pref_compat(nvs, "wifi_pass", nullptr, cfg.wifi_pass);
        load_string_pref_compat(nvs, "api_url", nullptr, cfg.api_url);
        load_string_pref_compat(nvs, "home_address", "home_addr", cfg.home_address);
        load_string_pref_compat(nvs, "weather_address", "weather_addr", cfg.weather_address);
        load_string_pref_compat(nvs, "transit_from", "sub_from", cfg.transit_from);
        load_string_pref_compat(nvs, "transit_to", "sub_to", cfg.transit_to);
        int32_t refresh = 15;
        if (nvs_get_i32(nvs, "refresh_minutes", &refresh) != ESP_OK) {
            nvs_get_i32(nvs, "refresh", &refresh);
        }
        cfg.refresh_minutes = clamp_refresh_minutes(refresh);
        nvs_close(nvs);
    }
}

static void save_device_config(const DeviceConfig &cfg) {
    nvs_handle_t nvs;
    if (nvs_open(CONFIG_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) return;
    save_string_pref(nvs, "wifi_ssid", cfg.wifi_ssid);
    save_string_pref(nvs, "wifi_pass", cfg.wifi_pass);
    save_string_pref(nvs, "api_url", cfg.api_url);
    save_string_pref(nvs, "home_address", cfg.home_address);
    save_string_pref(nvs, "weather_address", cfg.weather_address);
    save_string_pref(nvs, "transit_from", cfg.transit_from);
    save_string_pref(nvs, "transit_to", cfg.transit_to);
    nvs_set_i32(nvs, "refresh_minutes", clamp_refresh_minutes(cfg.refresh_minutes));
    nvs_commit(nvs);
    nvs_close(nvs);
}

static bool config_is_ready(const DeviceConfig &cfg) {
    return !cfg.wifi_ssid.empty() && !cfg.api_url.empty();
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data) {
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_count < 10) {
            esp_wifi_connect();
            s_retry_count++;
        } else {
            xEventGroupSetBits(s_wifi_events, WIFI_FAIL_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        s_retry_count = 0;
        xEventGroupSetBits(s_wifi_events, WIFI_CONNECTED_BIT);
    }
}

static void ensure_network_stack() {
    if (!s_network_ready) {
        esp_netif_init();
        esp_event_loop_create_default();
        esp_netif_create_default_wifi_sta();
        esp_netif_create_default_wifi_ap();
        s_network_ready = true;
    }
    if (!s_wifi_ready) {
        wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
        esp_wifi_init(&cfg);
        esp_wifi_set_storage(WIFI_STORAGE_RAM);
        s_wifi_events = xEventGroupCreate();
        esp_event_handler_instance_t h1, h2;
        esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL, &h1);
        esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL, &h2);
        s_wifi_ready = true;
    }
}

static bool wifi_connect_sta(const DeviceConfig &cfg) {
    ensure_network_stack();
    xEventGroupClearBits(s_wifi_events, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
    s_retry_count = 0;

    wifi_config_t wifi_cfg = {};
    strncpy(reinterpret_cast<char *>(wifi_cfg.sta.ssid), cfg.wifi_ssid.c_str(), sizeof(wifi_cfg.sta.ssid));
    strncpy(reinterpret_cast<char *>(wifi_cfg.sta.password), cfg.wifi_pass.c_str(), sizeof(wifi_cfg.sta.password));
    wifi_cfg.sta.threshold.authmode = WIFI_AUTH_OPEN;
    wifi_cfg.sta.pmf_cfg.capable = true;
    wifi_cfg.sta.pmf_cfg.required = false;

    esp_wifi_stop();
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    esp_wifi_start();

    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_events,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE,
        pdFALSE,
        pdMS_TO_TICKS(20000)
    );
    return (bits & WIFI_CONNECTED_BIT) != 0;
}

static esp_err_t config_root_get(httpd_req_t *req) {
    ESP_LOGI(TAG, "config_root_get enter");

    std::vector<char> page(12288, 0);

    int written = snprintf(
        page.data(), page.size(),
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light'>"
        "<title>M5Paper 本地配置</title>"
        "<style>:root{color-scheme:light;}html,body{background:#f5f7fa!important;color:#111827!important;}"
        "body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;padding:16px;}"
        ".card{max-width:760px;margin:0 auto;background:#fff;padding:20px 22px;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.08);}"
        "label{display:block;font-weight:600;margin:12px 0 6px;color:#111827;}input{width:100%%;box-sizing:border-box;padding:12px;border:1px solid #d0d5dd;border-radius:10px;font-size:15px;background:#fff!important;color:#111827!important;}"
        "button{margin-top:18px;background:#111827;color:#fff;border:none;padding:12px 18px;border-radius:10px;font-size:15px;}small{color:#667085;display:block;margin-top:4px;}"
        "</style></head><body><div class='card'><h1>M5Paper 本地配置</h1>"
        "<p>先填写 Wi-Fi 和后端地址。地址、天气、路线参数会作为 query 参数传给后端。</p>"
        "<form method='post' action='/save'>"
        "<label>Wi-Fi 名称</label><input name='wifi_ssid' value='%s'>"
        "<label>Wi-Fi 密码</label><input name='wifi_pass' type='password' value='%s'>"
        "<label>后端接口地址</label><input name='api_url' value='%s'><small>示例：http://192.168.1.100:8090/dashboard</small>"
        "<label>家庭地址</label><input name='home_address' value='%s'>"
        "<label>天气地址（可选）</label><input name='weather_address' value='%s'>"
        "<label>路线起点</label><input name='transit_from' value='%s'>"
        "<label>路线终点</label><input name='transit_to' value='%s'>"
        "<label>刷新间隔（分钟）</label><input name='refresh_minutes' value='%d'>"
        "<button type='submit'>保存并重启</button></form>"
        "</div></body></html>",
        g_cfg.wifi_ssid.c_str(),
        g_cfg.wifi_pass.c_str(),
        g_cfg.api_url.c_str(),
        g_cfg.home_address.c_str(),
        g_cfg.weather_address.c_str(),
        g_cfg.transit_from.c_str(),
        g_cfg.transit_to.c_str(),
        g_cfg.refresh_minutes
    );

    if (written < 0 || written >= (int)page.size()) {
        ESP_LOGE(TAG, "page build failed, written=%d, size=%d", written, (int)page.size());
        httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "page build failed");
        return ESP_FAIL;
    }

    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, page.data(), HTTPD_RESP_USE_STRLEN);
    ESP_LOGI(TAG, "config_root_get done");
    return ESP_OK;
}

static esp_err_t config_save_post(httpd_req_t *req) {
    std::vector<char> body(req->content_len + 1, 0);
    int received = 0;
    while (received < req->content_len) {
        int ret = httpd_req_recv(req, body.data() + received, req->content_len - received);
        if (ret <= 0) return ESP_FAIL;
        received += ret;
    }
    std::string form(body.data(), received);
    auto params = parse_form_encoded(form);

    DeviceConfig cfg = g_cfg;
    cfg.wifi_ssid = params["wifi_ssid"];
    cfg.wifi_pass = params["wifi_pass"];
    cfg.api_url = params["api_url"];
    cfg.home_address = params["home_address"];
    cfg.weather_address = params["weather_address"];
    cfg.transit_from = params.count("transit_from") ? params["transit_from"] : params["subway_from"];
    cfg.transit_to = params.count("transit_to") ? params["transit_to"] : params["subway_to"];
    cfg.refresh_minutes = params.count("refresh_minutes") ? clamp_refresh_minutes(atoi(params["refresh_minutes"].c_str())) : 15;

    if (cfg.wifi_ssid.empty() || cfg.api_url.empty()) {
        const char *resp = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><body style='font-family:sans-serif;padding:24px'><h2>保存失败</h2><p>Wi‑Fi 名称和 API_URL 不能为空。</p><p><a href='/'>返回继续填写</a></p></body></html>";
        httpd_resp_set_type(req, "text/html; charset=utf-8");
        httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
        return ESP_OK;
    }

    g_cfg = cfg;
    save_device_config(g_cfg);
    s_config_saved = true;

    const char *resp = "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta http-equiv='refresh' content='2;url=/'><body style='font-family:sans-serif;padding:24px'><h2>配置已保存</h2><p>设备即将重启并尝试联网。</p></body></html>";
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

static httpd_handle_t start_http_server() {
    ESP_LOGI(TAG, "start_http_server begin");

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.max_uri_handlers = 8;
    config.stack_size = 16384;

    httpd_handle_t server = nullptr;
    esp_err_t err = httpd_start(&server, &config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "httpd_start failed: %s", esp_err_to_name(err));
        return nullptr;
    }

    httpd_uri_t root = {
        .uri = "/",
        .method = HTTP_GET,
        .handler = config_root_get,
        .user_ctx = nullptr
    };

    httpd_uri_t save = {
        .uri = "/save",
        .method = HTTP_POST,
        .handler = config_save_post,
        .user_ctx = nullptr
    };

    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &root));
    ESP_ERROR_CHECK(httpd_register_uri_handler(server, &save));

    ESP_LOGI(TAG, "start_http_server ok, server=%p", server);
    return server;
}

static void stop_http_server() {
    if (s_httpd) {
        httpd_stop(s_httpd);
        s_httpd = nullptr;
    }
}

static bool should_enter_config_mode() {
    if (!M5.BtnA.isPressed()) return false;

    ESP_LOGI(TAG, "BtnA pressed, waiting for long press");
    const int64_t start_us = esp_timer_get_time();
    while (esp_timer_get_time() - start_us < 2000000) {
        M5.update();
        if (!M5.BtnA.isPressed()) {
            ESP_LOGI(TAG, "BtnA released before config timeout");
            return false;
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    ESP_LOGI(TAG, "BtnA long press confirmed");
    return true;
}

static void start_config_portal() {
    ESP_LOGI(TAG, "start_config_portal enter");

    ensure_network_stack();
    s_config_saved = false;
    esp_wifi_stop();

    wifi_config_t ap_cfg = {};
    strncpy(reinterpret_cast<char *>(ap_cfg.ap.ssid), AP_SSID_DEFAULT, sizeof(ap_cfg.ap.ssid));
    strncpy(reinterpret_cast<char *>(ap_cfg.ap.password), AP_PASS_DEFAULT, sizeof(ap_cfg.ap.password));
    ap_cfg.ap.ssid_len = strlen(AP_SSID_DEFAULT);
    ap_cfg.ap.channel = 1;
    ap_cfg.ap.max_connection = 4;
    ap_cfg.ap.authmode = WIFI_AUTH_WPA_WPA2_PSK;
    if (strlen(AP_PASS_DEFAULT) < 8) ap_cfg.ap.authmode = WIFI_AUTH_OPEN;

    ESP_LOGI(TAG, "set AP mode, ssid=%s", AP_SSID_DEFAULT);
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_cfg));

    ESP_LOGI(TAG, "starting wifi");
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "wifi started");

    s_httpd = start_http_server();
    ESP_LOGI(TAG, "http server handle=%p", s_httpd);

    auto &d = M5.Display;
    d.fillScreen(TFT_WHITE);
    d.setTextColor(TFT_BLACK);
    d.setTextDatum(top_left);
    d.setFont(&fonts::efontCN_24);
    d.drawString("进入配置模式", 40, 40);
    d.setFont(&fonts::efontCN_16);
    d.drawString("1. 手机连接热点：M5Paper-Setup", 40, 110);
    d.drawString("2. 密码：12345678", 40, 150);
    d.drawString("3. 浏览器打开：192.168.4.1", 40, 190);
    d.drawString("4. 保存后设备会自动重启", 40, 230);
    d.drawString("长按 A 键也可再次进入配置模式", 40, 300);

    int heartbeat = 0;
    while (true) {
        M5.update();

        if ((heartbeat++ % 15) == 0) {
            ESP_LOGI(TAG, "config portal alive, saved=%d", s_config_saved ? 1 : 0);
        }

        if (s_config_saved) {
            ESP_LOGI(TAG, "config saved, restarting");
            vTaskDelay(pdMS_TO_TICKS(1500));
            stop_http_server();
            esp_wifi_stop();
            esp_restart();
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

static esp_err_t http_event_handler(esp_http_client_event_t *evt) {
    if (evt->event_id == HTTP_EVENT_ON_DATA) {
        if (http_buf_len + evt->data_len < MAX_HTTP_BUF) {
            memcpy(http_buf + http_buf_len, evt->data, evt->data_len);
            http_buf_len += evt->data_len;
        }
    }
    return ESP_OK;
}

static cJSON *fetch_dashboard(const DeviceConfig &cfg) {
    http_buf = static_cast<char *>(malloc(MAX_HTTP_BUF));
    if (!http_buf) return nullptr;
    memset(http_buf, 0, MAX_HTTP_BUF);
    http_buf_len = 0;

    std::string url = cfg.api_url;
    url += (url.find('?') == std::string::npos) ? "?" : "&";
    url += "refresh_minutes=" + std::to_string(cfg.refresh_minutes);
    if (!cfg.home_address.empty()) url += "&home_address=" + url_encode(cfg.home_address);
    if (!cfg.weather_address.empty()) url += "&weather_address=" + url_encode(cfg.weather_address);
    if (!cfg.transit_from.empty()) url += "&transit_from=" + url_encode(cfg.transit_from);
    if (!cfg.transit_to.empty()) url += "&transit_to=" + url_encode(cfg.transit_to);

    esp_http_client_config_t http_cfg = {};
    http_cfg.url = url.c_str();
    http_cfg.event_handler = http_event_handler;
    http_cfg.timeout_ms = 15000;
    http_cfg.crt_bundle_attach = esp_crt_bundle_attach;

    esp_http_client_handle_t client = esp_http_client_init(&http_cfg);
    esp_err_t err = esp_http_client_perform(client);
    int status = esp_http_client_get_status_code(client);
    esp_http_client_cleanup(client);
    if (err != ESP_OK || status != 200) {
        free(http_buf);
        http_buf = nullptr;
        return nullptr;
    }

    http_buf[http_buf_len] = '\0';
    cJSON *json = cJSON_Parse(http_buf);
    free(http_buf);
    http_buf = nullptr;
    return json;
}

static const uint8_t *lookup_sprite(const char *name) {
    struct Entry { const char *name; const uint8_t *data; } map[] = {
        {"cap", sprite_cap}, {"nocap", sprite_nocap},
        {"shortsleeves", sprite_shortsleeves}, {"longsleeves", sprite_longsleeves},
        {"raincoat", sprite_raincoat}, {"shorts", sprite_shorts},
        {"pants", sprite_pants}, {"rainpants", sprite_rainpants},
        {"gloves", sprite_gloves}, {"mittens", sprite_mittens},
        {"nogloves", sprite_nogloves}, {"sneakers", sprite_sneakers},
        {"wintershoes", sprite_wintershoes},
    };
    for (auto &e : map) if (strcmp(name, e.name) == 0) return e.data;
    return nullptr;
}

static void drawSprite2bit(int x, int y, int w, int h, const uint8_t *data, uint16_t bg_color) {
    static bool lut_init = false;
    static uint16_t lut[3];
    if (!lut_init) {
        lut[0] = M5.Display.color565(26, 26, 26);
        lut[1] = M5.Display.color565(90, 90, 90);
        lut[2] = M5.Display.color565(176, 176, 176);
        lut_init = true;
    }
    uint16_t row_buf[SPRITE_W];
    for (int row = 0; row < h; row++) {
        for (int col = 0; col < w; col++) {
            int pixel_idx = row * w + col;
            int byte_idx = pixel_idx / 4;
            int shift = 6 - (pixel_idx % 4) * 2;
            uint8_t level = (data[byte_idx] >> shift) & 0x03;
            row_buf[col] = (level < 3) ? lut[level] : bg_color;
        }
        M5.Display.pushImage(x, y + row, w, 1, row_buf);
    }
}

static void drawSprite2bitScaled(
    int x,
    int y,
    int src_w,
    int src_h,
    int dst_w,
    int dst_h,
    const uint8_t *data,
    uint16_t bg_color
) {
    static bool lut_init = false;
    static uint16_t lut[3];
    if (!lut_init) {
        lut[0] = M5.Display.color565(26, 26, 26);
        lut[1] = M5.Display.color565(90, 90, 90);
        lut[2] = M5.Display.color565(176, 176, 176);
        lut_init = true;
    }

    std::vector<uint16_t> row_buf(dst_w);
    for (int row = 0; row < dst_h; row++) {
        int src_y = (row * src_h) / dst_h;
        for (int col = 0; col < dst_w; col++) {
            int src_x = (col * src_w) / dst_w;
            int pixel_idx = src_y * src_w + src_x;
            int byte_idx = pixel_idx / 4;
            int shift = 6 - (pixel_idx % 4) * 2;
            uint8_t level = (data[byte_idx] >> shift) & 0x03;
            row_buf[col] = (level < 3) ? lut[level] : bg_color;
        }
        M5.Display.pushImage(x, y + row, dst_w, 1, row_buf.data());
    }
}

static const char *json_string(cJSON *obj, const char *key, const char *fallback = "") {
    cJSON *item = cJSON_GetObjectItem(obj, key);
    return (item && cJSON_IsString(item) && item->valuestring) ? item->valuestring : fallback;
}

static int json_int(cJSON *obj, const char *key, int fallback = 0) {
    cJSON *item = cJSON_GetObjectItem(obj, key);
    return (item && cJSON_IsNumber(item)) ? item->valueint : fallback;
}

static float json_float(cJSON *obj, const char *key, float fallback = 0.0f) {
    cJSON *item = cJSON_GetObjectItem(obj, key);
    return (item && cJSON_IsNumber(item)) ? static_cast<float>(item->valuedouble) : fallback;
}

static void render_dashboard(cJSON *data) {
    auto &d = M5.Display;
    uint32_t bg = gray(255), black = gray(26), dark = gray(58), mid = gray(122), light = gray(176), faint = gray(208);
    d.fillScreen(bg);

    const char *timestamp = json_string(data, "timestamp", "--");
    const char *next_update = json_string(data, "next_update", "--");
    char buf[160];

    d.fillRect(0, 0, SCREEN_W, 52, black);
    d.setTextColor(bg);
    d.setTextDatum(top_left);
    d.setFont(&fonts::efontCN_24);
    d.drawString("家庭看板", 18, 10);
    d.setFont(&fonts::efontCN_14);
    snprintf(buf, sizeof(buf), "更新时间：%s", timestamp);
    d.drawString(buf, 180, 10);
    snprintf(buf, sizeof(buf), "下次刷新：%s", next_update);
    d.drawString(buf, 180, 28);

    int bat_pct = M5.Power.getBatteryLevel();
    int bat_x = SCREEN_W - 78, bat_y = 16;
    d.drawRect(bat_x, bat_y, 40, 18, bg);
    d.fillRect(bat_x + 40, bat_y + 5, 4, 8, bg);
    d.fillRect(bat_x + 2, bat_y + 2, (36 * bat_pct) / 100, 14, bg);
    snprintf(buf, sizeof(buf), "%d%%", bat_pct);
    d.drawString(buf, bat_x - 46, bat_y + 1);

    int left_x = 18, left_y = 70;
    int right_x = 548;

    const char *cond = json_string(data, "weather_condition", "unknown");
    const uint8_t *icon_data = weather_partly_cloudy;
    if (strcmp(cond, "clear") == 0 || strcmp(cond, "sunny") == 0) icon_data = weather_sunny;
    else if (strcmp(cond, "cloudy") == 0) icon_data = weather_cloudy;
    else if (strcmp(cond, "rainy") == 0) icon_data = weather_rainy;
    else if (strcmp(cond, "snowy") == 0) icon_data = weather_snowy;
    drawSprite2bit(left_x, left_y, WEATHER_ICON_W, WEATHER_ICON_H, icon_data, M5.Display.color565(255, 255, 255));

    cJSON *temp_j = cJSON_GetObjectItem(data, "temp_outdoor");
    d.setTextDatum(top_left);
    d.setFont(&fonts::FreeSansBold24pt7b);
    if (temp_j && !cJSON_IsNull(temp_j)) {
        snprintf(buf, sizeof(buf), "%.0f", temp_j->valuedouble);
        drawTemp(left_x + 106, left_y + 6, buf, black, 6);
    } else {
        drawTemp(left_x + 106, left_y + 6, "--", black, 6);
    }

    d.setFont(&fonts::efontCN_16);
    d.setTextColor(dark);
    snprintf(buf, sizeof(buf), "风速 %d km/h %s",
             json_int(data, "wind_kmh", 0),
             json_string(data, "wind_dir", ""));
    d.drawString(buf, left_x + 108, left_y + 62);

    cJSON *tmin_j = cJSON_GetObjectItem(data, "temp_min");
    cJSON *tmax_j = cJSON_GetObjectItem(data, "temp_max");
    float rain_max = json_float(data, "rain_max_mm", 0.0f);
    snprintf(buf, sizeof(buf), "最低 %.0f°  最高 %.0f°  降雨 %.1f mm",
             tmin_j ? tmin_j->valuedouble : 0.0, tmax_j ? tmax_j->valuedouble : 0.0, rain_max);
    d.drawString(buf, left_x + 108, left_y + 88);

    d.setTextColor(black);
    d.drawString(json_string(data, "weather_text", "暂无天气数据"), left_x + 108, left_y + 116);

    d.drawLine(18, 206, 522, 206, faint);
    d.setTextDatum(top_left);
    d.setFont(&fonts::efontCN_16);
    d.drawString("穿衣建议", 18, 218);
    cJSON *clothing = cJSON_GetObjectItem(data, "clothing");
    const int clothing_x = 18;
    const int clothing_y = 246;
    const int clothing_gap = 8;
    const int clothing_card_w = 94;
    const int clothing_card_h = 132;
    const int clothing_icon = 72;
    for (int i = 0; i < cJSON_GetArraySize(clothing) && i < 5; i++) {
        cJSON *item = cJSON_GetArrayItem(clothing, i);
        const char *sprite_name = cJSON_GetObjectItem(item, "sprite")->valuestring;
        const char *category = cJSON_GetObjectItem(item, "category")->valuestring;
        const char *label = cJSON_GetObjectItem(item, "label")->valuestring;
        int cx = clothing_x + i * (clothing_card_w + clothing_gap);
        int cy = clothing_y;
        d.drawRect(cx, cy, clothing_card_w, clothing_card_h, faint);
        d.fillRect(cx + 1, cy + 1, clothing_card_w - 2, clothing_icon + 8, gray(246));
        if (const uint8_t *spr = lookup_sprite(sprite_name)) {
            drawSprite2bitScaled(
                cx + (clothing_card_w - clothing_icon) / 2,
                cy + 6,
                SPRITE_W,
                SPRITE_H,
                clothing_icon,
                clothing_icon,
                spr,
                M5.Display.color565(246, 246, 246)
            );
        }
        d.setTextDatum(top_center);
        d.setFont(&fonts::efontCN_14);
        d.setTextColor(dark);
        d.drawString(utf8_truncate(category, 4).c_str(), cx + clothing_card_w / 2, cy + 80);
        d.setTextColor(black);
        std::string label_line1 = utf8_slice(label, 0, 5);
        std::string label_line2 = utf8_slice(label, 5, 5);
        d.drawString(label_line1.c_str(), cx + clothing_card_w / 2, cy + 96);
        d.drawString(label_line2.c_str(), cx + clothing_card_w / 2, cy + 112);
    }

    d.setTextDatum(top_left);
    d.setFont(&fonts::efontCN_16);
    int chart_x = right_x + 18, chart_y = 76, chart_w = 330, chart_h = 150;
    int chart_b = chart_y + chart_h;
    cJSON *temps_arr = cJSON_GetObjectItem(data, "temp_outdoor_24h");
    cJSON *rain_arr = cJSON_GetObjectItem(data, "rain_mm_24h");
    cJSON *hours_arr = cJSON_GetObjectItem(data, "hour_labels");
    float temps[24] = {}, rain[24] = {}; int hours[24] = {};
    float tmin = 999.f, tmax = -999.f, max_rain_scale = 10.f;
    for (int i = 0; i < 24 && i < cJSON_GetArraySize(temps_arr); i++) {
        temps[i] = (float)cJSON_GetArrayItem(temps_arr, i)->valuedouble;
        tmin = std::min(tmin, temps[i]);
        tmax = std::max(tmax, temps[i]);
    }
    for (int i = 0; i < 24 && i < cJSON_GetArraySize(rain_arr); i++) {
        rain[i] = (float)cJSON_GetArrayItem(rain_arr, i)->valuedouble;
        max_rain_scale = std::max(max_rain_scale, rain[i]);
    }
    for (int i = 0; i < 24 && i < cJSON_GetArraySize(hours_arr); i++) {
        hours[i] = cJSON_GetArrayItem(hours_arr, i)->valueint;
    }
    if (tmin > tmax) { tmin = -5; tmax = 25; }
    float cmin = std::min(tmin - 2.f, 0.f);
    float cmax = tmax + 3.f;
    float crange = std::max(1.f, cmax - cmin);

    d.setTextColor(black);
    d.drawString("温度与降雨（24小时）", right_x, 52);
    d.setTextDatum(middle_right);
    for (int t = (int)cmin; t <= (int)cmax; t += 5) {
        int y = chart_b - (int)(((t - cmin) / crange) * chart_h);
        snprintf(buf, sizeof(buf), "%d", t);
        d.drawString(buf, chart_x - 6, y);
        d.drawLine(chart_x, y, chart_x + chart_w, y, faint);
    }
    d.setTextDatum(middle_left);
    d.setTextColor(light);
    for (int mm = 0; mm <= (int)max_rain_scale; mm += std::max(1, (int)max_rain_scale / 5)) {
        int y = chart_b - (int)((mm / max_rain_scale) * chart_h);
        snprintf(buf, sizeof(buf), "%dmm", mm);
        d.drawString(buf, chart_x + chart_w + 6, y);
    }
    d.setTextDatum(top_center);
    d.setTextColor(mid);
    for (int i = 0; i < 24; i += 3) {
        int x = chart_x + (i * chart_w) / 23;
        snprintf(buf, sizeof(buf), "%02d", hours[i]);
        d.drawString(buf, x, chart_b + 4);
    }
    int prev_x = -1, prev_y = -1;
    for (int i = 0; i < 24; i++) {
        int x = chart_x + (i * chart_w) / 23;
        int bar_w = std::max(3, chart_w / 24 - 2);
        if (rain[i] > 0.05f) {
            int bar_h = std::max(2, (int)((rain[i] / max_rain_scale) * chart_h));
            d.fillRect(x - bar_w / 2, chart_b - bar_h, bar_w, bar_h, light);
        }
        int y = chart_b - (int)(((temps[i] - cmin) / crange) * chart_h);
        if (prev_x >= 0) {
            d.drawLine(prev_x, prev_y, x, y, black);
            d.drawLine(prev_x, prev_y - 1, x, y - 1, black);
        }
        prev_x = x;
        prev_y = y;
    }
    if (prev_x >= 0) {
        int first_y = chart_b - (int)(((temps[0] - cmin) / crange) * chart_h);
        d.fillCircle(chart_x, first_y, 5, black);
        d.fillCircle(chart_x, first_y, 2, bg);
        d.setTextDatum(top_left);
        d.setTextColor(black);
        d.drawString("现在", chart_x + 6, chart_y + 4);
    }

    int panel_y = 270;
    d.fillRect(right_x, panel_y, SCREEN_W - right_x - 12, 34, black);
    d.setTextColor(bg);
    d.setFont(&fonts::efontCN_16);
    d.drawString("路线信息", right_x + 10, panel_y + 8);
    std::string stop_name = utf8_truncate(json_string(data, "bus_stop_name", "通勤路线"), 10);
    d.drawString(stop_name.c_str(), right_x + 185, panel_y + 8);

    d.setTextColor(black);
    cJSON *deps = cJSON_GetObjectItem(data, "bus_departures");
    for (int i = 0; i < cJSON_GetArraySize(deps) && i < 5; i++) {
        cJSON *dep = cJSON_GetArrayItem(deps, i);
        int ey = panel_y + 42 + i * 44;
        if (i % 2 == 0) d.fillRect(right_x, ey, SCREEN_W - right_x - 12, 38, faint);
        d.fillRect(right_x + 8, ey + 6, 62, 26, black);
        d.setTextColor(bg);
        d.drawString(utf8_truncate(json_string(dep, "line", "--"), 4).c_str(), right_x + 15, ey + 10);
        d.setTextColor(black);
        d.drawString(utf8_truncate(json_string(dep, "dest", "暂无路线"), 10).c_str(), right_x + 78, ey + 8);
        d.setTextColor(dark);
        d.drawString(utf8_truncate(json_string(dep, "platform", ""), 12).c_str(), right_x + 78, ey + 24);
        d.setTextColor(black);
        d.drawString(json_string(dep, "time", "--"), SCREEN_W - 110, ey + 10);
    }
    if (!deps || cJSON_GetArraySize(deps) == 0) {
        d.setTextColor(black);
        d.drawString("暂无路线数据", right_x + 12, panel_y + 56);
        d.setTextColor(dark);
        d.drawString("未配置高德 Key 或当前查询失败", right_x + 12, panel_y + 86);
    }

    d.fillRect(0, SCREEN_H - 28, SCREEN_W, 28, faint);
    d.setTextColor(mid);
    bool wifi_ok = cJSON_IsTrue(cJSON_GetObjectItem(data, "wifi_ok"));
    bool weather_ok = cJSON_IsTrue(cJSON_GetObjectItem(data, "weather_api_ok"));
    bool route_ok = cJSON_IsTrue(cJSON_GetObjectItem(data, "bus_api_ok"));
    snprintf(buf, sizeof(buf), "WiFi %s  |  天气 %s  |  路线 %s",
             wifi_ok ? "正常" : "异常",
             weather_ok ? "正常" : "异常",
             route_ok ? "正常" : "异常");
    d.drawString(buf, 16, SCREEN_H - 22);
}

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "app_main start, reset_reason=%d", (int)esp_reset_reason());

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "nvs needs erase, ret=%s", esp_err_to_name(ret));
        nvs_flash_erase();
        nvs_flash_init();
    }

    ESP_LOGI(TAG, "before M5.begin");
    auto cfg = M5.config();
    M5.begin(cfg);
    ESP_LOGI(TAG, "after M5.begin");

    M5.Display.setRotation(1);
    M5.Display.setTextColor(TFT_BLACK);
    M5.Display.setTextDatum(middle_center);
    M5.Display.setFont(&fonts::efontCN_24);

    ESP_LOGI(TAG, "before load_device_config");
    load_device_config(g_cfg);
    ESP_LOGI(TAG, "after load_device_config, ssid_len=%d, api_len=%d",
             (int)g_cfg.wifi_ssid.size(), (int)g_cfg.api_url.size());

    vTaskDelay(pdMS_TO_TICKS(120));
    M5.update();

    bool force_config = should_enter_config_mode();
    bool ready = config_is_ready(g_cfg);
    ESP_LOGI(TAG, "force_config=%d ready=%d", force_config ? 1 : 0, ready ? 1 : 0);

    if (!ready || force_config) {
        ESP_LOGI(TAG, "entering config portal");
        start_config_portal();
        return;
    }

    ESP_LOGI(TAG, "connecting STA wifi");
    if (!wifi_connect_sta(g_cfg)) {
        ESP_LOGE(TAG, "wifi connect failed");
        M5.Display.fillScreen(TFT_WHITE);
        M5.Display.drawString("Wi-Fi 连接失败", SCREEN_W / 2, SCREEN_H / 2 - 20);
        M5.Display.setFont(&fonts::efontCN_16);
        M5.Display.drawString("按住 A 键重启可进入配置模式", SCREEN_W / 2, SCREEN_H / 2 + 24);
        vTaskDelay(pdMS_TO_TICKS(6000));
        esp_sleep_enable_timer_wakeup((uint64_t)10 * 60 * 1000000ULL);
        esp_deep_sleep_start();
        return;
    }

    ESP_LOGI(TAG, "fetching dashboard");
    cJSON *data = fetch_dashboard(g_cfg);
    if (!data) {
        ESP_LOGE(TAG, "fetch_dashboard failed");
        M5.Display.fillScreen(TFT_WHITE);
        M5.Display.drawString("接口请求失败", SCREEN_W / 2, SCREEN_H / 2 - 20);
        M5.Display.setFont(&fonts::efontCN_16);
        M5.Display.drawString("请检查 API_URL 和后端服务", SCREEN_W / 2, SCREEN_H / 2 + 24);
        vTaskDelay(pdMS_TO_TICKS(6000));
        esp_sleep_enable_timer_wakeup((uint64_t)10 * 60 * 1000000ULL);
        esp_deep_sleep_start();
        return;
    }

    ESP_LOGI(TAG, "rendering dashboard");
    render_dashboard(data);
    int sleep_minutes = cJSON_GetObjectItem(data, "sleep_minutes") ? cJSON_GetObjectItem(data, "sleep_minutes")->valueint : std::max(5, g_cfg.refresh_minutes);
    cJSON_Delete(data);

    ESP_LOGI(TAG, "sleep_minutes=%d", sleep_minutes);
    vTaskDelay(pdMS_TO_TICKS(5000));
    esp_wifi_stop();
    esp_sleep_enable_timer_wakeup((uint64_t)sleep_minutes * 60 * 1000000ULL);
    esp_deep_sleep_start();
}
