"""Gateway de nós de borda: gera a telemetria da rede e a envia pela API pública
(mesmo caminho dos ESP32 reais: POST /v1/readings com X-Device-Key).

Ciclo diário de temperatura/poluição + eventos climáticos aleatórios (chuva forte,
onda de calor, pico de particulado). Comandos manuais via Redis lista `emu:cmd`.
"""
import asyncio
import json
import math
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import redis.asyncio as aioredis

from .config import API_URL, EVENT_EVERY_SECONDS, NODE_TICK_SECONDS, REDIS_URL

SP = ZoneInfo("America/Sao_Paulo")
FULL = ("pm25", "temperature", "humidity", "water_level_pct")
FIRE_METRICS = ("temperature", "humidity", "smoke_ppm", "flame_detected")
# id -> (métricas que o nó possui, offset térmico °C, base de PM2.5, nível-base do bueiro %)
NODES = {
    "S-001": (FULL, 0.0, 16, 10), "S-002": (FULL, 0.5, 18, 12),
    "S-003": (("temperature", "humidity", "pm25"), 1.5, 22, 0),
    "S-004": (FULL, 1.0, 26, 14), "S-005": (("temperature", "humidity", "water_level_pct"), 1.2, 0, 16),
    "S-006": (FULL, 0.8, 14, 11), "S-007": (("temperature", "humidity", "pm25"), 0.6, 15, 0),
    "S-008": (FULL, 1.4, 21, 13), "S-009": (("temperature", "humidity", "water_level_pct"), 0.4, 0, 12),
    "S-010": (FULL, 0.7, 20, 10),
    "F-001": (FIRE_METRICS, 0.8, 0, 0), "F-002": (FIRE_METRICS, 0.4, 0, 0),
    "F-003": (FIRE_METRICS, 0.6, 0, 0), "F-004": (FIRE_METRICS, 1.0, 0, 0),
}
HAZ_METRIC = {"flood": "water_level_pct", "heat": "temperature", "air": "pm25", "fire": "smoke_ppm"}


def envelope(t: float, dur: float) -> float:
    """0→1 (subida) → patamar → 1→0 (recuo)."""
    x = t / dur
    if x < 0.35: return x / 0.35
    if x < 0.65: return 1.0
    return max(0.0, 1 - (x - 0.65) / 0.35)


def sample(node_id: str, at: datetime, ev: dict | None, now_s: float) -> dict:
    metrics, t_off, pm_base, w_base = NODES[node_id]
    h = at.astimezone(SP).hour + at.astimezone(SP).minute / 60
    temp = 22 + t_off + 5.5 * math.sin((h - 9) / 24 * 2 * math.pi) + random.gauss(0, 0.25)
    hum = 72 - (temp - 22) * 1.8 + random.gauss(0, 1.5)
    pm = pm_base * (1 + 0.5 * (math.exp(-((h - 8) ** 2) / 3) + math.exp(-((h - 18.5) ** 2) / 4))) + random.gauss(0, 1.2)
    water = w_base + random.gauss(0, 0.8)
    smoke = max(0, random.gauss(2.5, 1.0))
    flame = False
    if ev:
        k = envelope(now_s - ev["t0"], ev["dur"])
        if ev["kind"] == "flood":
            water += k * (88 - w_base)
        elif ev["kind"] == "heat":
            temp += k * (38.8 - temp + random.gauss(0, 0.3)) if temp < 38.8 else 0
            hum = hum * (1 - 0.1 * k) + 8 * k
        elif ev["kind"] == "air":
            pm += k * (95 + random.gauss(0, 4))
        elif ev["kind"] == "fire":
            smoke += k * (420 + random.gauss(0, 18))
            temp += k * 13
            flame = k > 0.45
    vals = {"temperature": round(temp, 1), "humidity": round(min(max(hum, 20), 98), 1),
            "pm25": round(max(pm, 2), 1), "water_level_pct": round(min(max(water, 0), 100), 1),
            "smoke_ppm": round(smoke, 1), "flame_detected": flame}
    return {m: vals[m] for m in metrics}


async def send(http: httpx.AsyncClient, node_id: str, values: dict, ts: datetime):
    body = {"sensor_id": node_id, "ts": ts.isoformat(), **values}
    for attempt in range(5):
        try:
            r = await http.post(f"{API_URL}/v1/readings", json=body, headers={"X-Device-Key": f"dev-key-{node_id}"}, timeout=8)
            if r.status_code < 500:
                return r.status_code
        except Exception:
            pass
        await asyncio.sleep(2 * (attempt + 1))


async def backfill(http, r, minutes: int = 45):
    """Preenche o histórico inicial e recupera leituras se o banco tiver sido recriado."""
    missing = set(NODES)
    try:
        response = await http.get(f"{API_URL}/v1/sensors", timeout=8)
        if response.is_success:
            rows = response.json()
            # Redis pode manter a marca de backfill mesmo depois de o volume do Postgres
            # ter sido recriado. Nesse caso, verifica o banco antes de pular o histórico.
            missing = {
                sensor["id"] for sensor in rows
                if sensor["id"] in NODES and not sensor.get("last_reading")
            }
            known = {sensor.get("id") for sensor in rows}
            missing.update(set(NODES) - known)
            if not rows:
                missing = set(NODES)
    except Exception:
        # A API pode ainda estar iniciando; tenta preencher os sensores conhecidos.
        missing = set(NODES)

    if not missing and await r.get("emu:backfilled"):
        return

    now = datetime.now(timezone.utc)
    failed = False
    for i in range(minutes, 0, -1):
        at = now - timedelta(minutes=i)
        for nid in missing:
            status = await send(http, nid, sample(nid, at, None, 0), at)
            if status is None or not 200 <= status < 300:
                failed = True
        if i % 5 == 0:
            await asyncio.sleep(1)  # não estoura o rate limit
    if not failed:
        await r.set("emu:backfilled", "1", ex=3600)


async def main():
    r = aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=30)
    events: dict[str, dict] = {}
    loop_t = asyncio.get_event_loop().time
    print(f"[nodes] {len(NODES)} nós ativos", flush=True)
    async with httpx.AsyncClient() as http:
        while not await r.get("emu:backfilled"):
            await backfill(http, r)
            if not await r.get("emu:backfilled"):
                print("[nodes] aguardando API/banco para completar o histórico inicial", flush=True)
                await asyncio.sleep(5)
        while True:
            now_s = loop_t()
            events = {k: v for k, v in events.items() if now_s - v["t0"] < v["dur"]}
            # comandos manuais (painel de operação / demonstração)
            while (raw := await r.lpop("emu:cmd")):
                c = json.loads(raw)
                if c["kind"] == "clear":
                    events.pop(c["sensor_id"], None)
                elif c["sensor_id"] in NODES and HAZ_METRIC[c["kind"]] in NODES[c["sensor_id"]][0]:
                    dur = c.get("dur", 130)
                    # Uma simulação manual de queimada substitui qualquer foco
                    # espontâneo ainda ativo. Assim, um clique representa um foco.
                    if c["kind"] == "fire":
                        events = {nid: ev for nid, ev in events.items() if ev["kind"] != "fire"}
                    events[c["sensor_id"]] = {"kind": c["kind"], "dur": dur,
                                              "t0": now_s - (0.5 * dur if c.get("instant") else 0)}
            # Eventos espontâneos ficam limitados a riscos climáticos gerais.
            # Queimadas são disparadas somente pelo controle explícito do painel,
            # evitando um segundo foco concorrente durante a demonstração.
            if len(events) < 2 and random.random() < NODE_TICK_SECONDS / EVENT_EVERY_SECONDS:
                nid = random.choice([n for n in NODES if n not in events])
                kind = random.choices(["flood", "heat", "air"], [0.40, 0.30, 0.30])[0]
                if HAZ_METRIC[kind] in NODES[nid][0]:
                    events[nid] = {"kind": kind, "t0": now_s, "dur": random.uniform(110, 170)}
            at = datetime.now(timezone.utc)
            await asyncio.gather(*(send(http, nid, sample(nid, at, events.get(nid), now_s), at) for nid in NODES))
            await asyncio.sleep(NODE_TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
