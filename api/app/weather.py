"""Previsão do tempo via Open-Meteo (gratuita, sem chave) com cache no Redis."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

FALLBACK = {"rain_last_1h_mm": 0.0, "rain_next_1h_mm": 0.0, "temp_next_3h": None,
            "wind_speed": 3.0, "source": "fallback"}


async def get_weather(r, http: httpx.AsyncClient, lat: float, lon: float) -> dict:
    key = f"wx:{lat:.2f}:{lon:.2f}"
    cached = await r.get(key)
    if cached:
        return json.loads(cached)
    try:
        resp = await http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
                    "current": "temperature_2m,wind_speed_10m",
                    "hourly": "temperature_2m,precipitation"},
            timeout=5,
        )
        resp.raise_for_status()
        j = resp.json()
        times = j["hourly"]["time"]
        idx = times.index(j["current"]["time"][:13] + ":00")
        precip, temps = j["hourly"]["precipitation"], j["hourly"]["temperature_2m"]
        wx = {
            "rain_last_1h_mm": float(precip[idx] or 0),
            "rain_next_1h_mm": float(precip[idx + 1] or 0) if idx + 1 < len(precip) else 0.0,
            "temp_next_3h": float(max(temps[idx: idx + 4])),
            "wind_speed": float(j["current"]["wind_speed_10m"]) / 3.6,  # km/h -> m/s
            "source": "open-meteo",
        }
        await r.set(key, json.dumps(wx), ex=900)
        return wx
    except Exception:
        await r.set(key, json.dumps(FALLBACK), ex=60)  # evita martelar a API externa
        return FALLBACK


def traffic_index(now: datetime | None = None) -> float:
    """Proxy de tráfego por horário (SP). Substituível por TomTom/Waze/CET."""
    h = (now or datetime.now(ZoneInfo("America/Sao_Paulo"))).hour
    if 7 <= h <= 9 or 17 <= h <= 20:
        return 0.9
    if 10 <= h <= 16:
        return 0.55
    return 0.2
