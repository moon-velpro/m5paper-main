"""M5PaperS3 中文看板模拟器。

从后端 /dashboard 拉取数据，渲染 960x540 的墨水屏风格预览图。
按 R 刷新，Q / Esc 退出。
"""

import os
import sys
from pathlib import Path

import pygame
import pygame.freetype
import requests

SCREEN_W = 960
SCREEN_H = 540
API_URL = os.getenv("M5PAPER_API_URL", "http://127.0.0.1:8090/dashboard")
ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")

BG = (232, 228, 216)
BLACK = (26, 26, 26)
DARK = (58, 58, 58)
MID = (122, 122, 122)
LIGHT = (176, 176, 168)
FAINT = (208, 205, 196)
FONT_CANDIDATES = [
    "microsoftyaheiui",
    "microsoftyahei",
    "simhei",
    "notosanscjksc",
    "pingfangsc",
    "heiti sc",
    "arialunicode",
]
FONT_PATH_CANDIDATES = [
    Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "msyh.ttc",
    Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "msyhbd.ttc",
    Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts" / "simhei.ttf",
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
]


def fetch_data() -> dict | None:
    try:
        resp = requests.get(API_URL, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"接口请求失败: {e}")
        return None


def load_weather_icons() -> dict[str, pygame.Surface]:
    icons = {}
    for name in ("sunny", "clear", "partly_cloudy", "cloudy", "rainy", "snowy"):
        path = os.path.join(ICONS_DIR, f"{name}.png")
        if os.path.exists(path):
            img = pygame.image.load(path).convert_alpha()
            icons[name] = pygame.transform.smoothscale(img, (80, 80))
    return icons


def fit_text(text: str, font: pygame.freetype.Font, max_width: int) -> str:
    current = text
    while current and font.get_rect(current).width > max_width:
        current = current[:-1]
    return current + "…" if current != text and current else current


def load_font(size: int, bold: bool = False) -> pygame.freetype.Font:
    for font_path in FONT_PATH_CANDIDATES:
        if font_path.exists():
            font = pygame.freetype.Font(str(font_path), size)
            font.strong = bold
            return font
    for candidate in FONT_CANDIDATES:
        try:
            font_path = pygame.freetype.match_font(candidate)
        except TypeError:
            font_path = None
        if font_path:
            font = pygame.freetype.Font(font_path, size)
            font.strong = bold
            return font
    fallback = pygame.freetype.Font(None, size)
    fallback.strong = bold
    return fallback


def draw_dashboard(screen: pygame.Surface, data: dict, fonts: dict, icons: dict):
    screen.fill(BG)
    f_sm = fonts["sm"]
    f_md = fonts["md"]
    f_lg = fonts["lg"]
    f_xl = fonts["xl"]
    f_title = fonts["title"]

    pygame.draw.rect(screen, BLACK, (0, 0, SCREEN_W, 56))
    f_title.render_to(screen, (20, 15), "家庭看板", BG)
    ts_text = f"更新时间：{data['timestamp']}  ·  下次刷新：{data['next_update']}"
    f_sm.render_to(screen, (250, 20), ts_text, BG)

    panel_y = 72
    panel_h = 160

    f_sm.render_to(screen, (30, panel_y + 6), "室外天气", DARK)
    to = data.get("temp_outdoor")
    f_xl.render_to(screen, (20, panel_y + 36), f"{to:.0f}°" if to is not None else "--°", BLACK)
    wind = data.get("wind_kmh")
    wdir = data.get("wind_dir") or ""
    f_sm.render_to(screen, (30, panel_y + 118), f"风速 {wind} km/h {wdir}" if wind is not None else "风速 --", MID)
    f_sm.render_to(screen, (30, panel_y + 138), data.get("weather_text", "暂无数据"), MID)

    for y in range(panel_y, panel_y + panel_h, 8):
        pygame.draw.line(screen, LIGHT, (215, y), (215, y + 4), 1)

    f_sm.render_to(screen, (235, panel_y + 6), "穿衣建议", DARK)
    clothing = data.get("clothing", [])
    for i, item in enumerate(clothing[:5]):
        y = panel_y + 34 + i * 22
        f_sm.render_to(screen, (235, y), f"{item.get('category', '')}：{item.get('label', '')}", BLACK)

    cond = data.get("weather_condition", "unknown")
    icon = icons.get(cond) or icons.get("partly_cloudy")
    if icon:
        screen.blit(icon, (430, panel_y + 10))

    tmin = data.get("temp_min")
    tmax = data.get("temp_max")
    minmax = f"最低 {tmin:.0f}°  最高 {tmax:.0f}°" if tmin is not None and tmax is not None else "最低 --  最高 --"
    f_md.render_to(screen, (430, panel_y + 100), minmax, DARK)
    f_sm.render_to(screen, (430, panel_y + 126), f"降雨峰值 {data.get('rain_max_mm', 0):.1f} mm", MID)

    pygame.draw.line(screen, LIGHT, (20, panel_y + panel_h + 8), (530, panel_y + panel_h + 8), 1)

    chart_x = 55
    chart_y = 270
    chart_w = 470
    chart_h = 175
    chart_bottom = chart_y + chart_h

    f_md.render_to(screen, (20, chart_y - 20), "温度与降雨（24小时）", BLACK)

    temps = data.get("temp_outdoor_24h", [0] * 24)
    rain = data.get("rain_mm_24h", [0] * 24)
    hour_labels = data.get("hour_labels", list(range(24)))

    valid_temps = [t for t in temps if t != 0]
    if valid_temps:
        temp_min_chart = min(min(valid_temps) - 2, 0)
        temp_max_chart = max(valid_temps) + 3
    else:
        temp_min_chart, temp_max_chart = -5, 25
    temp_range = max(temp_max_chart - temp_min_chart, 1)

    for t in range(int(temp_min_chart), int(temp_max_chart) + 1, 5):
        y = int(chart_bottom - ((t - temp_min_chart) / temp_range) * chart_h)
        f_sm.render_to(screen, (chart_x - 35, y - 5), f"{t}°", MID)
        pygame.draw.line(screen, FAINT, (chart_x, y), (chart_x + chart_w, y), 1)

    for i in range(0, 24, 3):
        x = chart_x + int((i / 23) * chart_w)
        f_sm.render_to(screen, (x - 8, chart_bottom + 6), f"{hour_labels[i]:02d}", MID)

    rain_scale = max(10.0, max(rain) if rain else 10.0)
    bar_w = max(chart_w // 24 - 2, 4)
    for i in range(24):
        if rain[i] > 0:
            x = chart_x + int((i / 23) * chart_w) - bar_w // 2
            bar_h = int((rain[i] / rain_scale) * chart_h)
            y = chart_bottom - bar_h
            color = LIGHT if rain[i] > rain_scale * 0.6 else FAINT
            pygame.draw.rect(screen, color, (x, y, bar_w, max(bar_h, 2)))

    for mm in range(0, int(rain_scale) + 1, max(1, int(rain_scale // 5) or 1)):
        y = int(chart_bottom - (mm / rain_scale) * chart_h)
        f_sm.render_to(screen, (chart_x + chart_w + 8, y - 5), f"{mm}mm", LIGHT)

    points_out = []
    for i in range(24):
        x = chart_x + int((i / 23) * chart_w)
        y = int(chart_bottom - ((temps[i] - temp_min_chart) / temp_range) * chart_h)
        points_out.append((x, y))
    if len(points_out) > 1:
        pygame.draw.lines(screen, BLACK, False, points_out, 3)

    now_x = chart_x + 2
    for dy in range(chart_y + 4, chart_bottom, 4):
        pygame.draw.line(screen, MID, (now_x, dy), (now_x, min(dy + 2, chart_bottom)), 1)
    f_sm.render_to(screen, (now_x + 4, chart_y + 2), "现在", BLACK)
    if points_out:
        pygame.draw.circle(screen, BLACK, points_out[0], 5)
        pygame.draw.circle(screen, BG, points_out[0], 2)

    leg_y = chart_bottom + 30
    pygame.draw.line(screen, BLACK, (chart_x, leg_y), (chart_x + 24, leg_y), 3)
    f_sm.render_to(screen, (chart_x + 30, leg_y - 5), "温度", DARK)
    pygame.draw.rect(screen, FAINT, (chart_x + 120, leg_y - 6, 16, 12))
    f_sm.render_to(screen, (chart_x + 142, leg_y - 5), "降雨 mm", DARK)

    bus_x = 570
    bus_y = 72
    bus_w = 370
    pygame.draw.line(screen, LIGHT, (bus_x - 20, 66), (bus_x - 20, SCREEN_H - 30), 2)

    pygame.draw.rect(screen, BLACK, (bus_x, bus_y, bus_w, 34))
    f_md.render_to(screen, (bus_x + 14, bus_y + 8), "路线信息", BG)
    stop_name = fit_text(data.get("bus_stop_name", ""), f_sm, 180)
    sw, _ = f_sm.get_rect(stop_name).size
    f_sm.render_to(screen, (bus_x + bus_w - sw - 12, bus_y + 12), stop_name, BG)

    departures = data.get("bus_departures", [])
    entry_h = 52
    max_dest_w = bus_w - 66 - 90 - 10

    if departures:
        for i, dep in enumerate(departures[:5]):
            ey = bus_y + 44 + i * entry_h
            if i % 2 == 0:
                pygame.draw.rect(screen, FAINT, (bus_x, ey, bus_w, entry_h - 2))

            badge_w, badge_h = 42, 28
            badge_x, badge_y = bus_x + 12, ey + 10
            pygame.draw.rect(screen, BLACK, (badge_x, badge_y, badge_w, badge_h))
            line_text = fit_text(dep.get("line", ""), f_md, badge_w - 4)
            lw, _ = f_md.get_rect(line_text).size
            f_md.render_to(screen, (badge_x + (badge_w - lw) // 2, badge_y + 5), line_text, BG)

            dest = fit_text(dep.get("dest", ""), f_md, max_dest_w)
            f_md.render_to(screen, (bus_x + 66, ey + 10), dest, BLACK)

            plat = fit_text(dep.get("platform", ""), f_sm, max_dest_w + 70)
            if plat:
                f_sm.render_to(screen, (bus_x + 66, ey + 32), plat, MID)

            time_str = dep.get("time", "")
            tw, _ = f_lg.get_rect(time_str).size
            f_lg.render_to(screen, (bus_x + bus_w - tw - 14, ey + 12), time_str, BLACK)
    else:
        pygame.draw.rect(screen, FAINT, (bus_x, bus_y + 44, bus_w, 90))
        f_md.render_to(screen, (bus_x + 16, bus_y + 72), "暂无路线数据", BLACK)
        f_sm.render_to(screen, (bus_x + 16, bus_y + 100), "未配置高德 Key 或当前查询失败", MID)

    foot_y = bus_y + 44 + len(departures[:5]) * entry_h + 16
    if not departures:
        foot_y = bus_y + 150
    f_sm.render_to(screen, (bus_x + 12, foot_y), f"面板数据时间 {data.get('timestamp', '--')} ", MID)

    pygame.draw.rect(screen, FAINT, (0, SCREEN_H - 28, SCREEN_W, 28))
    wifi_ok = "正常" if data.get("wifi_ok") else "异常"
    weather_ok = "正常" if data.get("weather_api_ok") else "异常"
    bus_ok = "正常" if data.get("bus_api_ok") else "异常"
    status = f"WiFi {wifi_ok}  ·  天气接口 {weather_ok}  ·  路线接口 {bus_ok}"
    f_sm.render_to(screen, (20, SCREEN_H - 20), status, MID)

    next_upd = f"下次刷新：{data['next_update']}"
    nw, _ = f_sm.get_rect(next_upd).size
    f_sm.render_to(screen, (SCREEN_W - nw - 20, SCREEN_H - 20), next_upd, MID)

    pygame.draw.rect(screen, (136, 136, 136), (0, 0, SCREEN_W, SCREEN_H), 2)


def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H))
    pygame.display.set_caption("M5PaperS3 中文看板模拟器")

    fonts = {
        "sm": load_font(13),
        "md": load_font(16, bold=True),
        "lg": load_font(22, bold=True),
        "xl": load_font(64, bold=True),
        "title": load_font(22, bold=True),
    }

    icons = load_weather_icons()
    data = fetch_data()
    if not data:
        print("获取数据失败，请确认后端已经运行在 8090 端口。")
        sys.exit(1)

    draw_dashboard(screen, data, fonts, icons)
    pygame.display.flip()
    pygame.image.save(screen, os.path.join(os.path.dirname(__file__), "dashboard_screenshot.png"))
    print("截图已保存到 simulator/dashboard_screenshot.png")

    if os.getenv("M5PAPER_SIM_ONESHOT") == "1":
        pygame.quit()
        return

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            if event.type == pygame.KEYDOWN:
                if event.key == pygame.K_r:
                    data = fetch_data()
                    if data:
                        draw_dashboard(screen, data, fonts, icons)
                        pygame.display.flip()
                        pygame.image.save(screen, os.path.join(os.path.dirname(__file__), "dashboard_screenshot.png"))
                        print("已刷新")
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False

    pygame.quit()


if __name__ == "__main__":
    main()
