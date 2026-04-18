from __future__ import annotations

import json
from datetime import datetime

from fastapi.testclient import TestClient
from zoneinfo import ZoneInfo

import main

ZRH = ZoneInfo("Europe/Zurich")


def test_get_refresh_schedule_weekday_midday():
    now = datetime(2026, 4, 6, 12, 0, tzinfo=ZRH)
    mode, minutes = main.get_refresh_schedule(now)
    assert mode == "low"
    assert minutes == 60


def test_get_refresh_schedule_weekend_afternoon():
    now = datetime(2026, 4, 11, 14, 0, tzinfo=ZRH)
    mode, minutes = main.get_refresh_schedule(now)
    assert mode == "high"
    assert minutes == 15


def test_effective_refresh_mode_respects_manual_interval():
    now = datetime(2026, 4, 6, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    mode, minutes = main.effective_refresh_mode(now, 30)
    assert mode == "low"
    assert minutes == 30


def test_strip_zurich_prefix():
    assert main.strip_zurich_prefix("Zürich, Hegibachplatz") == "Hegibachplatz"
    assert main.strip_zurich_prefix("Zürich Wiedikon, Bahnhof") == "Wiedikon, Bahnhof"
    assert main.strip_zurich_prefix("上海南站") == "上海南站"


def test_format_compact_departures_groups_times():
    result = main.format_compact_departures(
        [
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:17"},
            {"line": "31", "dest": "Zürich, Hegibachplatz", "time": "18:23"},
            {"line": "80", "dest": "Triemlispital", "time": "19:20"},
        ]
    )
    assert result[0].times == ["18:17", ":23"]
    assert result[0].dest == "Hegibachplatz"
    assert result[1].dest == "Triemlispital"


def test_admin_save_writes_json(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "dashboard_config.json")
    client = TestClient(main.app)

    response = client.post(
        "/admin/save",
        data={
            "home_address": "上海市徐汇区虹桥路",
            "weather_address": "31.188,121.437",
            "weather_latitude": "31.188",
            "weather_longitude": "121.437",
            "transit_from": "家",
            "transit_to": "公司",
            "refresh_minutes": "20",
            "amap_api_key": "demo-key",
            "city": "上海",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    saved = json.loads((tmp_path / "dashboard_config.json").read_text(encoding="utf-8"))
    assert saved["home_address"] == "上海市徐汇区虹桥路"
    assert saved["transit_to"] == "公司"
    assert saved["refresh_minutes"] == 20


def test_dashboard_returns_fallback_when_weather_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "dashboard_config.json")
    main.save_backend_config(
        main.BackendConfig(
            home_address="上海市徐汇区",
            weather_latitude=31.188,
            weather_longitude=121.437,
            transit_from="家",
            transit_to="公司",
            refresh_minutes=15,
        )
    )

    async def fake_fetch_weather(*args, **kwargs):
        raise RuntimeError("boom")

    async def fake_fetch_transit_summary(*args, **kwargs):
        return [], [], False

    monkeypatch.setattr(main, "fetch_weather", fake_fetch_weather)
    monkeypatch.setattr(main, "fetch_transit_summary", fake_fetch_transit_summary)

    client = TestClient(main.app)
    response = client.get("/dashboard")
    payload = response.json()

    assert response.status_code == 200
    assert payload["weather_api_ok"] is False
    assert payload["weather_text"] == "天气接口暂不可用"
    assert payload["bus_api_ok"] is False
    assert payload["bus_stop_name"] == "家→公司"


def test_dashboard_keeps_required_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "dashboard_config.json")
    main.save_backend_config(
        main.BackendConfig(
            home_address="上海市徐汇区",
            weather_latitude=31.188,
            weather_longitude=121.437,
            transit_from="家",
            transit_to="公司",
            refresh_minutes=10,
            amap_api_key="demo",
        )
    )

    async def fake_fetch_weather(*args, **kwargs):
        return {
            "temp_outdoor": 24.5,
            "wind_kmh": 12,
            "wind_dir": "东北",
            "temp_min": 20.0,
            "temp_max": 28.0,
            "rain_max_mm": 1.5,
            "weather_condition": "partly_cloudy",
            "weather_text": "多云转晴",
            "temp_outdoor_24h": [24.5] * 24,
            "rain_mm_24h": [0.0] * 24,
            "hour_labels": list(range(24)),
        }

    async def fake_fetch_transit_summary(*args, **kwargs):
        return (
            [main.BusDeparture(line="方案1", dest="地铁1号线→地铁9号线", time="42分", platform="步行620米 · 换乘1次")],
            [main.CompactBusDeparture(line="方案1", dest="地铁1号线/地铁9号线", times=["42分", "步行620米", "换乘1次"])],
            True,
        )

    monkeypatch.setattr(main, "fetch_weather", fake_fetch_weather)
    monkeypatch.setattr(main, "fetch_transit_summary", fake_fetch_transit_summary)

    client = TestClient(main.app)
    payload = client.get("/dashboard").json()

    required_keys = {
        "timestamp",
        "next_update",
        "refresh_mode",
        "sleep_minutes",
        "temp_outdoor",
        "wind_kmh",
        "wind_dir",
        "temp_min",
        "temp_max",
        "rain_max_mm",
        "weather_condition",
        "weather_text",
        "temp_outdoor_24h",
        "rain_mm_24h",
        "current_hour",
        "hour_labels",
        "clothing",
        "bus_stop_name",
        "bus_departures",
        "bus_departures_compact",
        "battery_pct",
        "wifi_ok",
        "weather_api_ok",
        "bus_api_ok",
    }
    assert required_keys.issubset(payload.keys())
    assert payload["weather_api_ok"] is True
    assert payload["bus_api_ok"] is True
    assert payload["bus_departures"][0]["time"] == "42分"
