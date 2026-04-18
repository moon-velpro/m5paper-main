#pragma once
// 仓库内置的默认兜底配置。
// 正式运行时如果存在 secrets.h，则优先使用 secrets.h；
// 如果没有，设备会因为占位符而自动进入 SoftAP 配置模式。

#define WIFI_SSID       "your-wifi-ssid"
#define WIFI_PASS       "your-wifi-password"
#define API_URL         "http://your-backend-ip:8090/dashboard"
