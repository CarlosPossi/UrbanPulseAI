import hashlib
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from typing import Literal
import random

from pydantic import BaseModel

from . import notify
from .config import ADMIN_KEY, ALERT_CHANNEL, DEMO_CONTROLS, RATE_LIMIT_PER_MIN, REDIS_URL, STREAM
from .emulator import HAZ_METRIC, NODES
from .db import create_pool
from .schemas import Accepted, ReadingIn

log = logging.getLogger("urbanpulse")

DESCRIPTION = """
**UrbanPulse AI** – rede descentralizada de micro-mitigação climática.

Fluxo: `POST /v1/readings` (IoT) → fila Redis Streams → workers com IA preditiva →
alerta + **ações reais** (WhatsApp/SMS, semáforos, irrigação) → mapa em tempo real (`/v1/stream`).

Autenticação dos dispositivos: header `X-Device-Key`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await create_pool()
    app.state.redis = aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=30, health_check_interval=15)
    yield
    await app.state.pool.close()
    await app.state.redis.aclose()


app = FastAPI(title="UrbanPulse AI", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def require_admin(x_admin_key: str = Header(...)):
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(403, "Chave de administrador inválida.")


# ------------------------------------------------------------------ ingestão
@app.post("/v1/readings", response_model=Accepted, status_code=202, tags=["Ingestão IoT"],
          summary="Recebe telemetria de um sensor")
async def ingest(req: Request, body: ReadingIn, x_device_key: str = Header(..., description="Chave do dispositivo")):
    pool, r = req.app.state.pool, req.app.state.redis
    ok = await pool.fetchval("SELECT 1 FROM sensors WHERE id=$1 AND api_key_hash=$2", body.sensor_id, _hash(x_device_key))
    if not ok:
        raise HTTPException(401, "Sensor desconhecido ou chave inválida.")

    bucket = f"rl:{body.sensor_id}:{int(time.time() // 60)}"
    n = await r.incr(bucket)
    if n == 1:
        await r.expire(bucket, 70)
    if n > RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Limite de requisições por minuto excedido para este sensor.")

    ts = body.ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    data = body.model_dump(mode="json") | {"ts": ts.isoformat()}
    sid = await r.xadd(STREAM, {"data": json.dumps(data)}, maxlen=100_000, approximate=True)
    return Accepted(stream_id=sid)


# ------------------------------------------------------------------ consulta
@app.get("/v1/sensors", tags=["Consulta"], summary="Status da rede de sensores (para o mapa)")
async def sensors(req: Request):
    rows = await req.app.state.pool.fetch("""
        SELECT s.id, s.name, ST_Y(s.geom::geometry) AS lat, ST_X(s.geom::geometry) AS lon, s.last_seen,
          (SELECT row_to_json(x) FROM (SELECT ts,pm25,temperature,humidity,water_level_pct,smoke_ppm,flame_detected
                                       FROM readings WHERE sensor_id=s.id ORDER BY ts DESC LIMIT 1) x) AS last_reading,
          COALESCE((SELECT array_agg(DISTINCT hazard) FROM alerts WHERE sensor_id=s.id AND resolved_at IS NULL), '{}') AS active_hazards
        FROM sensors s ORDER BY s.id""")
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        d = dict(row)
        d["online"] = bool(d["last_seen"] and (now - d["last_seen"]).total_seconds() < 300)
        out.append(d)
    return out


@app.get("/v1/sensors/{sensor_id}/readings", tags=["Consulta"], summary="Histórico recente de um sensor")
async def sensor_readings(req: Request, sensor_id: str, minutes: int = Query(60, ge=1, le=1440)):
    rows = await req.app.state.pool.fetch(
        "SELECT ts,pm25,temperature,humidity,water_level_pct,smoke_ppm,flame_detected FROM readings "
        "WHERE sensor_id=$1 AND ts > now() - make_interval(mins => $2) ORDER BY ts", sensor_id, minutes)
    return [dict(x) for x in rows]


@app.get("/v1/alerts", tags=["Consulta"], summary="Alertas com as ações automáticas disparadas")
async def alerts(req: Request, active: bool = True, limit: int = Query(50, ge=1, le=200)):
    rows = await req.app.state.pool.fetch("""
        SELECT a.id, a.sensor_id, a.hazard, a.risk, a.level, a.message, a.created_at, a.resolved_at,
               ST_Y(a.geom::geometry) AS lat, ST_X(a.geom::geometry) AS lon,
               COALESCE((SELECT json_agg(json_build_object('kind',x.kind,'target',t.name,'status',x.status,'lat',ST_Y(t.geom::geometry),'lon',ST_X(t.geom::geometry)) ORDER BY x.id)
                         FROM actions x JOIN action_targets t ON t.id=x.target_id WHERE x.alert_id=a.id), '[]'::json) AS actions,
               (SELECT row_to_json(rs) FROM (SELECT status, plan, units, started_at FROM responses WHERE alert_id=a.id) rs) AS response
        FROM alerts a WHERE (NOT $1::bool OR a.resolved_at IS NULL)
        ORDER BY a.created_at DESC LIMIT $2""", active, limit)
    return [dict(x) for x in rows]


@app.post("/v1/alerts/{alert_id}/resolve", tags=["Operação"], dependencies=[Depends(require_admin)],
          summary="Encerra manualmente um alerta (Defesa Civil)")
async def resolve(req: Request, alert_id: int):
    done = await req.app.state.pool.fetchval(
        "UPDATE alerts SET resolved_at=now() WHERE id=$1 AND resolved_at IS NULL RETURNING id", alert_id)
    if not done:
        raise HTTPException(404, "Alerta não encontrado ou já encerrado.")
    await req.app.state.redis.publish(ALERT_CHANNEL, json.dumps({"event": "resolved", "alert_id": alert_id}))
    return {"resolved": alert_id}


@app.get("/v1/stream", tags=["Consulta"], summary="Server-Sent Events: alertas em tempo real")
async def stream(req: Request):
    pubsub = req.app.state.redis.pubsub()
    await pubsub.subscribe(ALERT_CHANNEL)

    async def gen():
        try:
            while not await req.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15)
                yield f"data: {msg['data']}\n\n" if msg else ": ping\n\n"
        finally:
            await pubsub.aclose()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def queue_depth(r) -> int:
    """Mensagens ainda não processadas (lag do consumer group + pendentes)."""
    try:
        g = (await r.xinfo_groups(STREAM))[0]
        return int(g.get("lag") or 0) + int(g.get("pending") or 0)
    except Exception:
        return 0


@app.get("/v1/stats", tags=["Consulta"], summary="Indicadores da rede")
async def stats(req: Request):
    row = await req.app.state.pool.fetchrow("""
        SELECT (SELECT count(*) FROM sensors) AS sensors_total,
               (SELECT count(*) FROM sensors WHERE last_seen > now() - interval '5 minutes') AS sensors_online,
               (SELECT count(*) FROM alerts WHERE resolved_at IS NULL) AS alerts_active,
               (SELECT count(*) FROM actions WHERE created_at > now() - interval '24 hours') AS actions_24h,
               (SELECT count(*) FROM readings WHERE ts > now() - interval '1 hour') AS readings_1h""")
    return {**dict(row), "queue_depth": await queue_depth(req.app.state.redis), "server_time": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/ai/summary", tags=["IA simulada"], summary="Resumo inteligente das áreas monitoradas")
async def ai_summary(req: Request):
    """Resumo heurístico baseado nos alertas ativos e nas leituras mais recentes."""
    rows = await req.app.state.pool.fetch("""
        SELECT s.id, s.name, s.last_seen,
               a.hazard, a.risk, a.level, a.message, a.created_at,
               ST_Y(s.geom::geometry) AS lat, ST_X(s.geom::geometry) AS lon
        FROM sensors s
        LEFT JOIN alerts a ON a.sensor_id=s.id AND a.resolved_at IS NULL
        ORDER BY a.risk DESC NULLS LAST, s.name
    """)
    active = [dict(x) for x in rows if x["hazard"]]
    critical = [x for x in active if x["level"] == "critical"]
    by_hazard = {}
    for item in active:
        by_hazard[item["hazard"]] = by_hazard.get(item["hazard"], 0) + 1
    if not active:
        headline = "Nenhuma ocorrência ativa identificada."
        detail = "A rede segue monitorando os sensores. As áreas estão sem alertas ativos neste momento."
        priority = "normal"
    else:
        top = active[0]
        labels = {"flood": "alagamento", "heat": "calor extremo", "air": "qualidade do ar", "fire": "queimada / foco de incêndio"}
        headline = f"{len(active)} ocorrência(s) ativa(s) em monitoramento."
        detail = (f"A área prioritária é {top['name']}, com risco de {labels.get(top['hazard'], top['hazard'])} "
                  f"estimado em {top['risk']:.0%}. "
                  f"Foram identificados {len(critical)} caso(s) crítico(s).")
        priority = "critical" if critical else "warning"
    return {
        "mode": "simulated_ai", "headline": headline, "summary": detail,
        "priority": priority, "active_count": len(active), "critical_count": len(critical),
        "distribution": by_hazard,
        "areas": [{
            "sensor_id": x["id"], "name": x["name"], "hazard": x["hazard"],
            "risk": x["risk"], "level": x["level"], "lat": x["lat"], "lon": x["lon"]
        } for x in active[:5]],
        "generated_at": datetime.now(timezone.utc).isoformat()
    }


@app.get("/v1/targets", tags=["Consulta"], summary="Destinatários e atuadores cadastrados")
async def targets(req: Request):
    rows = await req.app.state.pool.fetch(
        "SELECT id, kind, name, radius_m, hazards, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon "
        "FROM action_targets WHERE active ORDER BY id")
    return [dict(x) for x in rows]


@app.get("/v1/vulnerable", tags=["Consulta"], summary="Pontos de vulnerabilidade (escolas, asilos, UBS)")
async def vulnerable(req: Request):
    rows = await req.app.state.pool.fetch(
        "SELECT id, name, kind, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon FROM vulnerable_points ORDER BY id")
    return [dict(x) for x in rows]


@app.post("/v1/admin/scenario", include_in_schema=False, dependencies=[Depends(require_admin)])
async def trigger_scenario(req: Request, sensor_id: str, kind: str = Query(..., pattern="^(flood|heat|air|fire|clear)$")):
    """Força um evento num nó (ensaio da demonstração)."""
    await req.app.state.redis.rpush("emu:cmd", json.dumps({"sensor_id": sensor_id, "kind": kind}))
    return {"queued": True, "sensor_id": sensor_id, "kind": kind}


@app.get("/v1/config", tags=["Operação"], summary="Recursos habilitados no painel")
async def config():
    return {"demo_controls": DEMO_CONTROLS, "telegram": notify.configured(), "telegram_residents": notify.residents_configured(),
            "server_time": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/bases", tags=["Consulta"], summary="Bases das equipes de resposta")
async def bases(req: Request):
    rows = await req.app.state.pool.fetch(
        "SELECT id, name, kind, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon FROM response_bases ORDER BY id")
    return [dict(x) for x in rows]


@app.get("/v1/ai/feed", tags=["IA de resposta"], summary="Diário de decisões da IA")
async def ai_feed(req: Request, limit: int = Query(40, ge=1, le=200)):
    rows = await req.app.state.pool.fetch(
        "SELECT id, alert_id, ts, kind, text FROM ai_log ORDER BY id DESC LIMIT $1", limit)
    return [dict(x) for x in reversed(rows)]


class IncidentIn(BaseModel):
    hazard: Literal["flood", "heat", "air", "fire", "random"] = "random"
    sensor_id: str | None = None


@app.post("/v1/demo/incident", tags=["Demonstração"], summary="Simula uma ocorrência (botão do painel)")
async def demo_incident(req: Request, body: IncidentIn):
    """Injeta leituras críticas pelo mesmo pipeline dos sensores; a IA reage como a qualquer alerta real."""
    if not DEMO_CONTROLS:
        raise HTTPException(404, "Controles de demonstração desativados.")
    r, pool = req.app.state.redis, req.app.state.pool
    if not await r.set("demo:cooldown", "1", nx=True, ex=8):
        raise HTTPException(429, "Aguarde um instante antes de simular outra ocorrência.")
    busy = {(x["sensor_id"], x["hazard"]) for x in await pool.fetch("SELECT sensor_id, hazard FROM alerts WHERE resolved_at IS NULL")}
    hazards = list(HAZ_METRIC) if body.hazard == "random" else [body.hazard]
    random.shuffle(hazards)
    options = [(s, h) for h in hazards for s in NODES
               if HAZ_METRIC[h] in NODES[s][0] and (s, h) not in busy and (body.sensor_id in (None, s))]
    if not options:
        raise HTTPException(409, "Este sensor já tem uma ocorrência ativa desse tipo ou não mede esse risco.")
    sid, haz = options[0] if body.sensor_id else random.choice([o for o in options if o[1] == hazards[0]] or options)
    # Um único caminho de disparo: o gateway do ESP simulado gera e envia
    # a leitura crítica no próximo ciclo. Não inserir também diretamente no
    # Redis Stream, pois isso duplicaria a mesma ocorrência.
    dur = 150
    # Bloqueia cliques repetidos durante a mesma janela de simulação.
    # O emulador também não cria focos espontâneos de queimada.
    await r.rpush("emu:cmd", json.dumps({"sensor_id": sid, "kind": haz, "instant": True, "dur": dur}))
    name = await pool.fetchval("SELECT name FROM sensors WHERE id=$1", sid)
    return {"sensor_id": sid, "name": name, "hazard": haz}


@app.post("/v1/notify/test", tags=["Operação"], dependencies=[Depends(require_admin)], summary="Testa o Telegram")
async def notify_test():
    if not notify.configured():
        raise HTTPException(409, "Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
    async with httpx.AsyncClient() as http:
        return {"sent_to": await notify.send_test(http)}


@app.get("/health", tags=["Operação"])
async def health(req: Request):
    await req.app.state.pool.fetchval("SELECT 1")
    await req.app.state.redis.ping()
    return {"status": "ok", "queue_depth": await queue_depth(req.app.state.redis)}


# ------------------------------------------------------------------ mocks dos atuadores (demo)
# Em produção, aponte action_targets.endpoint para o gateway real (ver README).
@app.post("/mock/{kind}", tags=["Mocks de atuadores"], include_in_schema=True,
          summary="Simula o webhook de WhatsApp / semáforo / irrigação")
async def mock_actuator(kind: str, payload: dict):
    if kind not in ("whatsapp", "traffic", "irrigation"):
        raise HTTPException(404)
    log.warning("[MOCK %s] %s", kind.upper(), json.dumps(payload, ensure_ascii=False))
    return {"accepted": True, "kind": kind}


# ------------------------------------------------------------------ frontend (dashboard)
_front = Path(__file__).resolve().parent.parent / "frontend"
if _front.is_dir():
    app.mount("/", StaticFiles(directory=_front, html=True), name="frontend")
