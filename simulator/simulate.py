#!/usr/bin/env python3
"""Simula os nós IoT (sem dependências). Uso:
   python simulator/simulate.py --scenario flood --sensor S-001
   cenários: normal | flood | heat | air | fire
"""
import argparse, json, random, time, urllib.request, urllib.error
from datetime import datetime, timezone


def reading(sensor, scenario, step):
    r = {"sensor_id": sensor, "ts": datetime.now(timezone.utc).isoformat(),
         "temperature": round(random.uniform(22, 27), 1), "humidity": round(random.uniform(55, 70), 1),
         "pm25": round(random.uniform(8, 20), 1), "water_level_pct": round(random.uniform(5, 20), 1), "smoke_ppm": round(random.uniform(0, 5), 1), "flame_detected": False}
    if scenario == "flood":      # bueiro enchendo rápido
        r["water_level_pct"] = min(100, 30 + step * 9 + random.uniform(-2, 2))
    elif scenario == "heat":     # onda de calor
        r["temperature"], r["humidity"] = round(37 + random.uniform(0, 3), 1), round(random.uniform(60, 80), 1)
    elif scenario == "air":      # pico de particulado
        r["pm25"] = round(70 + step * 8 + random.uniform(-3, 3), 1)
    elif scenario == "fire":     # fumaça + detecção de chama
        r["smoke_ppm"] = round(350 + step * 12 + random.uniform(-10, 10), 1)
        r["temperature"] = round(38 + step * 0.5 + random.uniform(-1, 1), 1)
        r["flame_detected"] = step >= 2
    return r


def post(url, key, body):
    req = urllib.request.Request(f"{url}/v1/readings", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Device-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return f"{e.code} {e.read().decode()[:120]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--sensor", default="S-001")
    ap.add_argument("--scenario", default="normal", choices=["normal", "flood", "heat", "air", "fire"])
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()
    for i in range(a.count):
        body = reading(a.sensor, a.scenario, i)
        print(f"[{i+1}/{a.count}] {a.scenario} -> {post(a.url, f'dev-key-{a.sensor}', body)}  {json.dumps(body)}")
        time.sleep(a.interval)
