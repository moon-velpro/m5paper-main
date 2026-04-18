from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

LOCAL_TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))
CONFIG_PATH = Path(os.getenv("CONFIG_FILE", Path(__file__).with_name("dashboard_config.json")))
REQUEST_TIMEOUT = 20.0
DEFAULT_REFRESH_MINUTES = 15
DEFAULT_BUS_STOP_NAME = "通勤路线"
COORDINATE_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*$")

app = FastAPI(title="M5Paper 中文看板 API")

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
COMPASS_ZH = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
WEATHER_TEXT_MAP: dict[int, tuple[str, str]] = {
    0: ("clear", "晴"),
    1: ("partly_cloudy", "少云"),
    2: ("partly_cloudy", "多云"),
    3: ("cloudy", "阴"),
    45: ("cloudy", "有雾"),
    48: ("cloudy", "冻雾"),
    51: ("rainy", "毛毛雨"),
    53: ("rainy", "小雨"),
    55: ("rainy", "中雨"),
    56: ("rainy", "冻毛毛雨"),
    57: ("rainy", "冻雨"),
    61: ("rainy", "小雨"),
    63: ("rainy", "中雨"),
    65: ("rainy", "大雨"),
    66: ("rainy", "冻雨"),
    67: ("rainy", "强冻雨"),
    71: ("snowy", "小雪"),
    73: ("snowy", "中雪"),
    75: ("snowy", "大雪"),
    77: ("snowy", "冰粒"),
    80: ("rainy", "阵雨"),
    81: ("rainy", "强阵雨"),
    82: ("rainy", "暴雨"),
    85: ("snowy", "阵雪"),
    86: ("snowy", "强阵雪"),
    95: ("rainy", "雷阵雨"),
    96: ("rainy", "伴有冰雹的雷阵雨"),
    99: ("rainy", "强雷暴"),
}


@dataclass
class BackendConfig:
    home_address: str = os.getenv("HOME_ADDRESS", "上海")
    weather_address: str = os.getenv("WEATHER_ADDRESS", "")
    weather_latitude: float | None = None
    weather_longitude: float | None = None
    transit_from: str = os.getenv("TRANSIT_FROM", os.getenv("SUBWAY_FROM", ""))
    transit_to: str = os.getenv("TRANSIT_TO", os.getenv("SUBWAY_TO", ""))
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    amap_api_key: str = os.getenv("AMAP_API_KEY", "")
    city: str = os.getenv("CITY_NAME", "")


class BusDeparture(BaseModel):
    line: str
    dest: str
    time: str
    platform: str


class CompactBusDeparture(BaseModel):
    line: str
    dest: str
    times: list[str]


class ClothingItem(BaseModel):
    category: str
    sprite: str
    label: str


class DashboardData(BaseModel):
    timestamp: str
    next_update: str
    refresh_mode: str
    sleep_minutes: int
    temp_outdoor: float | None
    wind_kmh: int | None
    wind_dir: str | None
    temp_min: float | None
    temp_max: float | None
    rain_max_mm: float
    weather_condition: str
    weather_text: str
    temp_outdoor_24h: list[float]
    rain_mm_24h: list[float]
    current_hour: int
    hour_labels: list[int]
    clothing: list[ClothingItem]
    bus_stop_name: str
    bus_departures: list[BusDeparture]
    bus_departures_compact: list[CompactBusDeparture]
    battery_pct: int
    wifi_ok: bool
    weather_api_ok: bool
    bus_api_ok: bool


HIDDEN_DESTINATIONS = {"Zürich, Dunkelhölzli"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def clamp_refresh_minutes(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_REFRESH_MINUTES
    return max(5, min(parsed, 240))


def parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def escape_html(value: Any) -> str:
    return html.escape(clean_text(value), quote=True)


def format_timestamp_zh(dt: datetime) -> str:
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d} {WEEKDAY_ZH[dt.weekday()]} {dt.hour:02d}:{dt.minute:02d}"


def deg_to_compass(deg: float | None) -> str | None:
    if deg is None:
        return None
    return COMPASS_ZH[int((deg + 22.5) / 45) % 8]


def weather_code_to_condition(code: int | None) -> tuple[str, str]:
    if code is None:
        return "unknown", "暂无天气数据"
    return WEATHER_TEXT_MAP.get(code, ("cloudy", "天气多变"))


def parse_coordinate_text(value: str) -> tuple[float, float] | None:
    match = COORDINATE_RE.match(clean_text(value))
    if not match:
        return None
    first = float(match.group(1))
    second = float(match.group(2))
    if abs(first) <= 90 and abs(second) <= 180:
        return first, second
    if abs(second) <= 90 and abs(first) <= 180:
        return second, first
    return None


def strip_zurich_prefix(dest: str) -> str:
    if dest.startswith("Zürich, "):
        return dest[len("Zürich, "):]
    if dest.startswith("Zürich ") and not dest.startswith("Zürichs"):
        return dest[len("Zürich "):]
    return dest


def format_compact_departures(departures: list[dict[str, Any]]) -> list[CompactBusDeparture]:
    groups: dict[tuple[str, str], list[str]] = {}
    for dep in departures:
        raw_dest = clean_text(dep.get("dest", "?"))
        if raw_dest in HIDDEN_DESTINATIONS:
            continue
        line = clean_text(dep.get("line", "?"))
        dest = strip_zurich_prefix(raw_dest)
        time = clean_text(dep.get("time", "??:??"))
        groups.setdefault((line, dest), []).append(time)

    result: list[CompactBusDeparture] = []
    for (line, dest), times in groups.items():
        formatted: list[str] = []
        prev_hour = None
        for time_text in times:
            hour = time_text[:2] if len(time_text) >= 5 else None
            if prev_hour is None or hour != prev_hour:
                formatted.append(time_text)
            else:
                formatted.append(time_text[2:])
            prev_hour = hour
        result.append(CompactBusDeparture(line=line, dest=dest, times=formatted))
    return result


def build_bus_stop_name(transit_from: str, transit_to: str) -> str:
    if transit_from and transit_to:
        return f"{transit_from}→{transit_to}"
    if transit_to:
        return transit_to
    if transit_from:
        return transit_from
    return DEFAULT_BUS_STOP_NAME


def normalize_config(data: dict[str, Any] | None) -> BackendConfig:
    raw = data or {}
    return BackendConfig(
        home_address=clean_text(raw.get("home_address", raw.get("address", os.getenv("HOME_ADDRESS", "上海")))),
        weather_address=clean_text(raw.get("weather_address", os.getenv("WEATHER_ADDRESS", ""))),
        weather_latitude=parse_optional_float(raw.get("weather_latitude", raw.get("latitude", os.getenv("WEATHER_LATITUDE")))),
        weather_longitude=parse_optional_float(raw.get("weather_longitude", raw.get("longitude", os.getenv("WEATHER_LONGITUDE")))),
        transit_from=clean_text(raw.get("transit_from", raw.get("subway_from", os.getenv("TRANSIT_FROM", os.getenv("SUBWAY_FROM", ""))))),
        transit_to=clean_text(raw.get("transit_to", raw.get("subway_to", os.getenv("TRANSIT_TO", os.getenv("SUBWAY_TO", ""))))),
        refresh_minutes=clamp_refresh_minutes(raw.get("refresh_minutes", os.getenv("REFRESH_MINUTES", DEFAULT_REFRESH_MINUTES))),
        amap_api_key=clean_text(raw.get("amap_api_key", os.getenv("AMAP_API_KEY", ""))),
        city=clean_text(raw.get("city", os.getenv("CITY_NAME", ""))),
    )


def load_backend_config() -> BackendConfig:
    if not CONFIG_PATH.exists():
        return normalize_config(None)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("读取配置失败，使用默认值: %s", exc)
        return normalize_config(None)
    return normalize_config(data)


def save_backend_config(cfg: BackendConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(asdict(normalize_config(asdict(cfg))), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_refresh_schedule(now: datetime) -> tuple[str, int]:
    hour = now.hour
    is_weekend = now.weekday() >= 5
    if hour >= 21 or hour < 5:
        if hour >= 21:
            minutes_until_5 = (24 - hour + 5) * 60 - now.minute
        else:
            minutes_until_5 = (5 - hour) * 60 - now.minute
        return ("sleep", minutes_until_5)
    if 5 <= hour < 9:
        return ("high", 15)
    if is_weekend and 9 <= hour < 17:
        return ("high", 15)
    return ("low", 60)


def effective_refresh_mode(now: datetime, refresh_minutes: int) -> tuple[str, int]:
    schedule_mode, schedule_minutes = get_refresh_schedule(now)
    if schedule_mode == "sleep":
        return schedule_mode, schedule_minutes
    effective_minutes = clamp_refresh_minutes(refresh_minutes)
    return ("high" if effective_minutes <= 15 else "low", effective_minutes)


def build_fallback_weather(now: datetime, reason: str) -> dict[str, Any]:
    return {
        "temp_outdoor": None,
        "wind_kmh": None,
        "wind_dir": None,
        "temp_min": None,
        "temp_max": None,
        "rain_max_mm": 0.0,
        "weather_condition": "unknown",
        "weather_text": reason,
        "temp_outdoor_24h": [0.0] * 24,
        "rain_mm_24h": [0.0] * 24,
        "hour_labels": [((now.hour + i) % 24) for i in range(24)],
    }


async def geocode_with_open_meteo(address: str) -> tuple[float, float] | None:
    query = clean_text(address)
    if not query:
        return None
    coords = parse_coordinate_text(query)
    if coords is not None:
        return coords

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": query, "count": 1, "language": "zh", "format": "json"},
        )
        response.raise_for_status()
        payload = response.json()

    results = payload.get("results") or []
    if not results:
        return None
    first = results[0]
    return float(first["latitude"]), float(first["longitude"])


async def amap_geocode(address: str, key: str, city: str = "") -> tuple[str, str] | None:
    query = clean_text(address)
    api_key = clean_text(key)
    if not (query and api_key):
        return None

    coords = parse_coordinate_text(query)
    if coords is not None:
        latitude, longitude = coords
        return f"{longitude:.6f}", f"{latitude:.6f}"

    params = {"address": query, "key": api_key}
    if city:
        params["city"] = clean_text(city)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get("https://restapi.amap.com/v3/geocode/geo", params=params)
        response.raise_for_status()
        payload = response.json()

    if payload.get("status") != "1" or not payload.get("geocodes"):
        return None
    location = payload["geocodes"][0].get("location", "")
    if "," not in location:
        return None
    longitude, latitude = location.split(",", 1)
    return longitude, latitude


async def resolve_weather_coordinates(
    cfg: BackendConfig,
    home_address: str = "",
    weather_address: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
) -> tuple[float, float] | None:
    if latitude is not None and longitude is not None:
        return latitude, longitude

    if cfg.weather_latitude is not None and cfg.weather_longitude is not None:
        return cfg.weather_latitude, cfg.weather_longitude

    candidates: list[str] = []
    seen: set[str] = set()
    for raw_candidate in (weather_address, home_address, cfg.weather_address, cfg.home_address):
        candidate = clean_text(raw_candidate)
        if not candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)

    for candidate in candidates:
        parsed = parse_coordinate_text(candidate)
        if parsed is not None:
            return parsed
        resolved = await geocode_with_open_meteo(candidate)
        if resolved is not None:
            return resolved

    if cfg.amap_api_key:
        for candidate in candidates:
            resolved = await amap_geocode(candidate, cfg.amap_api_key, cfg.city)
            if resolved is None:
                continue
            longitude, latitude_text = resolved
            return float(latitude_text), float(longitude)

    return None


async def fetch_weather(lat: float, lon: float, now: datetime | None = None) -> dict[str, Any]:
    current_time = now or datetime.now(LOCAL_TZ)
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": str(LOCAL_TZ),
        "current": "temperature_2m,weather_code,wind_speed_10m,wind_direction_10m",
        "hourly": "temperature_2m,precipitation,weather_code,wind_speed_10m,wind_direction_10m",
        "forecast_days": 2,
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get("https://api.open-meteo.com/v1/forecast", params=params)
        response.raise_for_status()
        payload = response.json()

    current = payload.get("current", {})
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    rain = hourly.get("precipitation", [])

    current_hour_key = current_time.strftime("%Y-%m-%dT%H")
    start_idx = 0
    for index, time_text in enumerate(times):
        if str(time_text).startswith(current_hour_key):
            start_idx = index
            break

    temps_24h = [round(float(value), 1) for value in temps[start_idx:start_idx + 24]]
    rain_24h = [round(float(value), 1) for value in rain[start_idx:start_idx + 24]]
    if len(temps_24h) < 24:
        fill_value = temps_24h[-1] if temps_24h else 0.0
        temps_24h.extend([fill_value] * (24 - len(temps_24h)))
    if len(rain_24h) < 24:
        rain_24h.extend([0.0] * (24 - len(rain_24h)))

    temp_outdoor = current.get("temperature_2m")
    wind_speed = current.get("wind_speed_10m")
    weather_code = current.get("weather_code")
    weather_condition, weather_text = weather_code_to_condition(weather_code)

    return {
        "temp_outdoor": round(float(temp_outdoor), 1) if temp_outdoor is not None else None,
        "wind_kmh": round(float(wind_speed)) if wind_speed is not None else None,
        "wind_dir": deg_to_compass(current.get("wind_direction_10m")),
        "temp_min": min(temps_24h) if temps_24h else None,
        "temp_max": max(temps_24h) if temps_24h else None,
        "rain_max_mm": max(rain_24h) if rain_24h else 0.0,
        "weather_condition": weather_condition,
        "weather_text": weather_text,
        "temp_outdoor_24h": temps_24h,
        "rain_mm_24h": rain_24h,
        "hour_labels": [((current_time.hour + i) % 24) for i in range(24)],
    }


def recommend_clothing(temp_min: float | None, rain_max_mm: float, wind_kmh: int | None) -> list[ClothingItem]:
    base_temp = temp_min if temp_min is not None else 18.0
    feels_like = base_temp - (wind_kmh or 0) * 0.08
    rain_heavy = rain_max_mm >= 3.0
    rain_light = rain_max_mm >= 1.0

    if feels_like < 5:
        head = ClothingItem(category="头部", sprite="cap", label="保暖帽")
    elif rain_light:
        head = ClothingItem(category="头部", sprite="cap", label="带帽更稳妥")
    else:
        head = ClothingItem(category="头部", sprite="nocap", label="无需特别加帽")

    if rain_light:
        top = ClothingItem(category="上装", sprite="raincoat", label="防水外套")
    elif feels_like < 12:
        top = ClothingItem(category="上装", sprite="longsleeves", label="长袖外套")
    else:
        top = ClothingItem(category="上装", sprite="shortsleeves", label="短袖或薄上衣")

    if rain_heavy:
        bottom = ClothingItem(category="下装", sprite="rainpants", label="防雨长裤")
    elif feels_like < 14:
        bottom = ClothingItem(category="下装", sprite="pants", label="长裤")
    else:
        bottom = ClothingItem(category="下装", sprite="shorts", label="轻薄下装")

    if feels_like < 8:
        hands = ClothingItem(category="手部", sprite="gloves", label="建议戴手套")
    else:
        hands = ClothingItem(category="手部", sprite="nogloves", label="手套可不带")

    if rain_heavy or feels_like < 6:
        shoes = ClothingItem(category="鞋子", sprite="wintershoes", label="防水鞋")
    else:
        shoes = ClothingItem(category="鞋子", sprite="sneakers", label="运动鞋")

    return [head, top, bottom, hands, shoes]


def compact_line_name(name: str) -> str:
    cleaned = clean_text(name)
    if not cleaned:
        return ""
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    cleaned = cleaned.split("(", 1)[0].strip()
    return cleaned


def extract_transit_lines(transit: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for segment in transit.get("segments", []):
        bus = segment.get("bus") or {}
        for line in bus.get("buslines") or []:
            name = compact_line_name(line.get("name", ""))
            if name and name not in lines:
                lines.append(name)
        railway = segment.get("railway") or {}
        railway_name = compact_line_name(railway.get("name", ""))
        if railway_name and railway_name not in lines:
            lines.append(railway_name)
    return lines


def summarize_transit_plan(transit: dict[str, Any], transit_to: str, index: int) -> tuple[BusDeparture, CompactBusDeparture]:
    duration_seconds = int(float(transit.get("duration", 0) or 0))
    walking_distance = int(float(transit.get("walking_distance", 0) or 0))
    lines = extract_transit_lines(transit)
    if not lines:
        lines = ["步行为主"]

    transfer_count = max(0, len(lines) - 1)
    duration_text = f"{max(1, round(duration_seconds / 60))}分"
    walk_text = f"步行{walking_distance}米"
    transfer_text = f"换乘{transfer_count}次"
    title = "→".join(lines[:3])
    line_badge = f"方案{index}"

    departure = BusDeparture(
        line=line_badge,
        dest=title or clean_text(transit_to) or DEFAULT_BUS_STOP_NAME,
        time=duration_text,
        platform=f"{walk_text} · {transfer_text}",
    )
    compact = CompactBusDeparture(
        line=line_badge,
        dest="/".join(lines[:3]),
        times=[duration_text, walk_text, transfer_text],
    )
    return departure, compact


async def fetch_transit_summary(
    cfg: BackendConfig,
    transit_from: str,
    transit_to: str,
) -> tuple[list[BusDeparture], list[CompactBusDeparture], bool]:
    start = clean_text(transit_from)
    end = clean_text(transit_to)
    if not (cfg.amap_api_key and start and end):
        return [], [], False

    try:
        origin = await amap_geocode(start, cfg.amap_api_key, cfg.city)
        destination = await amap_geocode(end, cfg.amap_api_key, cfg.city)
        if not origin or not destination:
            return [], [], False

        params = {
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{destination[0]},{destination[1]}",
            "city": clean_text(cfg.city) or None,
            "extensions": "base",
            "strategy": "0",
            "key": cfg.amap_api_key,
        }
        params = {key: value for key, value in params.items() if value}

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get("https://restapi.amap.com/v3/direction/transit/integrated", params=params)
            response.raise_for_status()
            payload = response.json()

        if payload.get("status") != "1":
            return [], [], False

        transits = ((payload.get("route") or {}).get("transits")) or []
        if not transits:
            return [], [], False

        departures: list[BusDeparture] = []
        compact: list[CompactBusDeparture] = []
        for index, transit in enumerate(transits[:3], start=1):
            dep, dep_compact = summarize_transit_plan(transit, end, index)
            departures.append(dep)
            compact.append(dep_compact)
        return departures, compact, True
    except Exception as exc:
        log.warning("路线查询失败: %s", exc)
        return [], [], False


def render_admin_html(cfg: BackendConfig, saved: bool = False, error: str = "") -> str:
    message = ""
    if saved:
        message = "<p class='notice ok'>配置已保存。</p>"
    if error:
        message = f"<p class='notice error'>{escape_html(error)}</p>"

    example_dashboard = "/dashboard?home_address=" + quote_plus(cfg.home_address)
    return f"""<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>M5Paper 中文后台管理</title>
<style>
:root {{
  --bg:#f4f7fb;
  --card:#ffffff;
  --line:#d7deea;
  --text:#111827;
  --muted:#667085;
  --accent:#0f766e;
  --danger:#b42318;
}}
body {{
  margin:0;
  background:linear-gradient(180deg,#eef4ff 0%,var(--bg) 40%,#eef3f6 100%);
  color:var(--text);
  font-family:"Microsoft YaHei UI","PingFang SC","Noto Sans CJK SC",sans-serif;
}}
.wrap {{
  max-width:920px;
  margin:0 auto;
  padding:28px 18px 40px;
}}
.card {{
  background:var(--card);
  border:1px solid rgba(15,23,42,.05);
  border-radius:20px;
  padding:22px 22px 18px;
  box-shadow:0 10px 30px rgba(15,23,42,.08);
  margin-bottom:18px;
}}
h1,h2 {{
  margin:0 0 12px;
}}
p {{
  line-height:1.6;
}}
.grid {{
  display:grid;
  gap:14px 16px;
  grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
}}
label {{
  display:block;
  font-weight:700;
  margin-bottom:6px;
}}
input {{
  width:100%;
  box-sizing:border-box;
  padding:12px 14px;
  font-size:15px;
  border:1px solid var(--line);
  border-radius:12px;
  background:#fff;
}}
small {{
  display:block;
  margin-top:6px;
  color:var(--muted);
}}
.actions {{
  display:flex;
  gap:12px;
  flex-wrap:wrap;
  margin-top:18px;
}}
button,a.button {{
  border:none;
  background:var(--text);
  color:#fff;
  border-radius:12px;
  padding:12px 18px;
  font-size:15px;
  cursor:pointer;
  text-decoration:none;
}}
a.button.secondary {{
  background:#fff;
  color:var(--text);
  border:1px solid var(--line);
}}
.notice {{
  margin:0 0 14px;
  padding:12px 14px;
  border-radius:12px;
}}
.ok {{
  background:#ecfdf3;
  color:#027a48;
}}
.error {{
  background:#fef3f2;
  color:var(--danger);
}}
.mono {{
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  background:#f4f4f5;
  padding:2px 6px;
  border-radius:6px;
}}
</style>
</head>
<body>
<div class='wrap'>
  <div class='card'>
    <h1>M5Paper 中文后台管理</h1>
    <p>这里保存天气与路线配置。设备端 SoftAP 配置页只负责 Wi‑Fi 和 API 地址；本页负责家庭地址、天气坐标、路线起终点和可选的高德 Key。</p>
    {message}
    <form method='post' action='/admin/save'>
      <div class='grid'>
        <div>
          <label for='home_address'>家庭地址</label>
          <input id='home_address' name='home_address' value='{escape_html(cfg.home_address)}' placeholder='例如：上海市徐汇区虹桥路'>
          <small>/dashboard 默认使用这里的地址。</small>
        </div>
        <div>
          <label for='weather_address'>天气地址</label>
          <input id='weather_address' name='weather_address' value='{escape_html(cfg.weather_address)}' placeholder='可选，不填则使用家庭地址'>
          <small>可填文本地址，也可直接填“纬度,经度”。</small>
        </div>
        <div>
          <label for='weather_latitude'>天气纬度（可选）</label>
          <input id='weather_latitude' name='weather_latitude' value='{"" if cfg.weather_latitude is None else cfg.weather_latitude}' placeholder='例如 31.188'>
        </div>
        <div>
          <label for='weather_longitude'>天气经度（可选）</label>
          <input id='weather_longitude' name='weather_longitude' value='{"" if cfg.weather_longitude is None else cfg.weather_longitude}' placeholder='例如 121.437'>
        </div>
        <div>
          <label for='transit_from'>路线起点</label>
          <input id='transit_from' name='transit_from' value='{escape_html(cfg.transit_from)}' placeholder='例如：家'>
        </div>
        <div>
          <label for='transit_to'>路线终点</label>
          <input id='transit_to' name='transit_to' value='{escape_html(cfg.transit_to)}' placeholder='例如：公司 / 地铁站'>
        </div>
        <div>
          <label for='refresh_minutes'>刷新间隔（分钟）</label>
          <input id='refresh_minutes' name='refresh_minutes' value='{cfg.refresh_minutes}' placeholder='建议 15'>
        </div>
        <div>
          <label for='city'>高德城市（可选）</label>
          <input id='city' name='city' value='{escape_html(cfg.city)}' placeholder='例如：上海'>
        </div>
        <div style='grid-column:1/-1'>
          <label for='amap_api_key'>高德 Key（可选）</label>
          <input id='amap_api_key' name='amap_api_key' value='{escape_html(cfg.amap_api_key)}' placeholder='配置后可启用公共交通路线'>
          <small>没有 Key 或查询失败时，/dashboard 会返回空路线并将 <span class='mono'>bus_api_ok</span> 设为 false，不会崩溃。</small>
        </div>
      </div>
      <div class='actions'>
        <button type='submit'>保存配置</button>
        <a class='button secondary' href='/dashboard' target='_blank'>查看 /dashboard</a>
        <a class='button secondary' href='{example_dashboard}' target='_blank'>按家庭地址预览</a>
      </div>
    </form>
  </div>
  <div class='card'>
    <h2>设备端接口地址</h2>
    <p>设备本地配置页中的 <span class='mono'>API_URL</span> 建议填写为：<span class='mono'>http://你的服务器IP:8090/dashboard</span></p>
    <p>路线信息兼容字段映射：<span class='mono'>bus_departures.dest</span> 放推荐方案标题，<span class='mono'>time</span> 放总时长，<span class='mono'>platform</span> 放步行距离与换乘次数；<span class='mono'>bus_departures_compact.dest</span> 放主要线路名。</p>
  </div>
</div>
</body>
</html>"""


def build_dashboard_response(
    now: datetime,
    weather: dict[str, Any],
    refresh_mode: str,
    refresh_minutes: int,
    bus_stop_name: str,
    bus_departures: list[BusDeparture],
    bus_departures_compact: list[CompactBusDeparture],
    weather_api_ok: bool,
    bus_api_ok: bool,
) -> DashboardData:
    next_update = (now + timedelta(minutes=refresh_minutes)).strftime("%H:%M")
    clothing = recommend_clothing(weather.get("temp_min"), weather.get("rain_max_mm", 0.0), weather.get("wind_kmh"))
    hour_labels = weather.get("hour_labels", [((now.hour + i) % 24) for i in range(24)])

    return DashboardData(
        timestamp=format_timestamp_zh(now),
        next_update=next_update,
        refresh_mode=refresh_mode,
        sleep_minutes=refresh_minutes,
        temp_outdoor=weather.get("temp_outdoor"),
        wind_kmh=weather.get("wind_kmh"),
        wind_dir=weather.get("wind_dir"),
        temp_min=weather.get("temp_min"),
        temp_max=weather.get("temp_max"),
        rain_max_mm=float(weather.get("rain_max_mm", 0.0) or 0.0),
        weather_condition=clean_text(weather.get("weather_condition", "unknown")) or "unknown",
        weather_text=clean_text(weather.get("weather_text", "暂无天气数据")) or "暂无天气数据",
        temp_outdoor_24h=[float(value) for value in weather.get("temp_outdoor_24h", [0.0] * 24)],
        rain_mm_24h=[float(value) for value in weather.get("rain_mm_24h", [0.0] * 24)],
        current_hour=now.hour,
        hour_labels=[int(value) for value in hour_labels],
        clothing=clothing,
        bus_stop_name=bus_stop_name,
        bus_departures=bus_departures,
        bus_departures_compact=bus_departures_compact,
        battery_pct=100,
        wifi_ok=True,
        weather_api_ok=weather_api_ok,
        bus_api_ok=bus_api_ok,
    )


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/admin")


@app.get("/admin", response_class=HTMLResponse)
async def admin(saved: int = 0, error: str = "") -> str:
    cfg = load_backend_config()
    return render_admin_html(cfg, saved=bool(saved), error=error)


@app.post("/admin/save")
async def admin_save(
    home_address: str = Form(""),
    weather_address: str = Form(""),
    weather_latitude: str = Form(""),
    weather_longitude: str = Form(""),
    transit_from: str = Form(""),
    transit_to: str = Form(""),
    refresh_minutes: str = Form(str(DEFAULT_REFRESH_MINUTES)),
    amap_api_key: str = Form(""),
    city: str = Form(""),
) -> RedirectResponse:
    latitude = parse_optional_float(weather_latitude)
    longitude = parse_optional_float(weather_longitude)
    if (weather_latitude.strip() and latitude is None) or (weather_longitude.strip() and longitude is None):
        return RedirectResponse(url="/admin?error=天气经纬度格式不正确", status_code=303)

    cfg = BackendConfig(
        home_address=clean_text(home_address),
        weather_address=clean_text(weather_address),
        weather_latitude=latitude,
        weather_longitude=longitude,
        transit_from=clean_text(transit_from),
        transit_to=clean_text(transit_to),
        refresh_minutes=clamp_refresh_minutes(refresh_minutes),
        amap_api_key=clean_text(amap_api_key),
        city=clean_text(city),
    )
    save_backend_config(cfg)
    return RedirectResponse(url="/admin?saved=1", status_code=303)


@app.get("/api/config")
async def api_config() -> JSONResponse:
    return JSONResponse(asdict(load_backend_config()))


@app.get("/dashboard", response_model=DashboardData)
async def dashboard(
    home_address: str = Query("", description="临时覆盖家庭地址"),
    address: str = Query("", description="兼容旧参数：家庭地址"),
    weather_address: str = Query("", description="临时覆盖天气地址"),
    latitude: float | None = Query(None, description="临时覆盖天气纬度"),
    longitude: float | None = Query(None, description="临时覆盖天气经度"),
    transit_from: str = Query("", description="临时覆盖路线起点"),
    transit_to: str = Query("", description="临时覆盖路线终点"),
    subway_from: str = Query("", description="兼容旧参数：路线起点"),
    subway_to: str = Query("", description="兼容旧参数：路线终点"),
    refresh_minutes: int | None = Query(None, description="临时覆盖刷新间隔"),
) -> DashboardData:
    now = datetime.now(LOCAL_TZ)
    cfg = load_backend_config()

    effective_home_address = clean_text(home_address or address or cfg.home_address)
    effective_weather_address = clean_text(weather_address or cfg.weather_address)
    effective_transit_from = clean_text(transit_from or subway_from or cfg.transit_from)
    effective_transit_to = clean_text(transit_to or subway_to or cfg.transit_to)
    effective_refresh = clamp_refresh_minutes(refresh_minutes if refresh_minutes is not None else cfg.refresh_minutes)
    refresh_mode, next_minutes = effective_refresh_mode(now, effective_refresh)
    bus_stop_name = build_bus_stop_name(effective_transit_from, effective_transit_to)

    weather_api_ok = True
    try:
        coords = await resolve_weather_coordinates(
            cfg,
            home_address=effective_home_address,
            weather_address=effective_weather_address,
            latitude=latitude,
            longitude=longitude,
        )
        if coords is None:
            raise ValueError("未能解析天气坐标，请在 /admin 填写地址或经纬度")
        weather = await fetch_weather(coords[0], coords[1], now=now)
    except Exception as exc:
        weather_api_ok = False
        log.warning("天气查询失败: %s", exc)
        weather = build_fallback_weather(now, "天气接口暂不可用")

    bus_departures, bus_departures_compact, bus_api_ok = await fetch_transit_summary(
        cfg,
        effective_transit_from,
        effective_transit_to,
    )

    return build_dashboard_response(
        now=now,
        weather=weather,
        refresh_mode=refresh_mode,
        refresh_minutes=next_minutes,
        bus_stop_name=bus_stop_name,
        bus_departures=bus_departures,
        bus_departures_compact=bus_departures_compact,
        weather_api_ok=weather_api_ok,
        bus_api_ok=bus_api_ok,
    )
