
"""Worker assíncrono: consome o Redis Stream, roda os modelos de risco, abre alertas,
dispara as ações automáticas e aciona a IA de resposta.

Escala horizontalmente: docker compose up --scale worker=3
"""

import asyncio
import json
import os
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
import httpx
import redis.asyncio as aioredis

from . import ml
from .actions import dispatch
from .config import (
    ALERT_CHANNEL,
    ALERT_THRESHOLD,
    CRITICAL_THRESHOLD,
    GROUP,
    REDIS_URL,
    RESOLVE_THRESHOLD,
    STREAM,
)
from .db import create_pool
from .response import ACTIVE, publish, recover, run_response
from .weather import get_weather, traffic_index


CONSUMER = os.getenv("HOSTNAME", socket.gethostname())
BACKGROUND_TASKS: set = set()


STATE_SQL = """
SELECT
    (array_agg(pm25 ORDER BY ts DESC)
        FILTER (WHERE pm25 IS NOT NULL))[1] AS pm25,

    (array_agg(pm25 ORDER BY ts ASC)
        FILTER (
            WHERE pm25 IS NOT NULL
            AND ts > now() - interval '15 minutes'
        ))[1] AS pm25_old,

    (array_agg(temperature ORDER BY ts DESC)
        FILTER (WHERE temperature IS NOT NULL))[1] AS temp,

    (array_agg(humidity ORDER BY ts DESC)
        FILTER (WHERE humidity IS NOT NULL))[1] AS hum,

    (array_agg(water_level_pct ORDER BY ts DESC)
        FILTER (WHERE water_level_pct IS NOT NULL))[1] AS water,

    (array_agg(water_level_pct ORDER BY ts ASC)
        FILTER (
            WHERE water_level_pct IS NOT NULL
            AND ts > now() - interval '15 minutes'
        ))[1] AS water_old,

    (array_agg(smoke_ppm ORDER BY ts DESC)
        FILTER (WHERE smoke_ppm IS NOT NULL))[1] AS smoke,

    (array_agg(flame_detected ORDER BY ts DESC)
        FILTER (WHERE flame_detected IS NOT NULL))[1] AS flame

FROM readings
WHERE sensor_id = $1
AND ts > now() - interval '30 minutes'
"""


TARGETS_SQL = """
SELECT t.id, t.kind, t.name, t.contact, t.endpoint
FROM action_targets t
JOIN alerts a ON a.id = $1
WHERE t.active
AND $2::text = ANY(t.hazards)
AND ST_DWithin(t.geom, a.geom, t.radius_m)
"""


LABEL = {
    "flood": "ALAGAMENTO",
    "heat": "ESTRESSE TÉRMICO",
    "air": "POLUIÇÃO DO AR",
    "fire": "QUEIMADA / FOCO DE INCÊNDIO",
}


TIP = {
    "flood": "Evite a via, não atravesse áreas alagadas e retire objetos do térreo.",
    "heat": "Hidrate-se, mantenha crianças e idosos em locais frescos e ventilados.",
    "air": "Evite exercícios ao ar livre; quem tem asma ou doença respiratória deve ficar em ambiente fechado.",
    "fire": "Afaste-se da fumaça e do foco de incêndio, não tente combater as chamas, mantenha rotas de acesso livres e ligue 193 (Bombeiros) em caso de perigo imediato.",
}


async def evaluate(pool, r, http, sid, name, hazard, risk, feats):

    # A verificação e a criação do alerta acontecem sob o mesmo lock.
    # Isso impede que workers diferentes criem alertas simultâneos
    # para o mesmo sensor e tipo de risco.

    try:
        async with pool.acquire() as c:
            async with c.transaction():

                # Lock transacional por sensor + tipo de risco.
                # É liberado automaticamente quando a transação termina.
                await c.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended($1, 0)
                    )
                    """,
                    f"{sid}:{hazard}",
                )

                active = await c.fetchrow(
                    """
                    SELECT a.id, rs.status
                    FROM alerts a
                    LEFT JOIN responses rs ON rs.alert_id = a.id
                    WHERE a.sensor_id = $1
                    AND a.hazard = $2
                    AND a.resolved_at IS NULL
                    """,
                    sid,
                    hazard,
                )

                if active:

                    # Enquanto as equipes estão em campo,
                    # quem encerra é a resposta, não a telemetria.
                    if (
                        risk < RESOLVE_THRESHOLD
                        and active["status"] not in ACTIVE
                    ):
                        await c.execute(
                            """
                            UPDATE alerts
                            SET resolved_at = now()
                            WHERE id = $1
                            """,
                            active["id"],
                        )

                        await publish(
                            r,
                            {
                                "event": "resolved",
                                "alert_id": active["id"],
                            },
                        )

                    return

                # Não abre alerta abaixo do limite ou durante cooldown.
                if (
                    risk < ALERT_THRESHOLD
                    or await r.get(
                        f"resolved-cooldown:{sid}:{hazard}"
                    )
                ):
                    return

                level = (
                    "critical"
                    if risk >= CRITICAL_THRESHOLD
                    else "warning"
                )

                vul = await c.fetch(
                    """
                    SELECT v.name
                    FROM vulnerable_points v
                    JOIN sensors s ON s.id = $1
                    WHERE ST_DWithin(v.geom, s.geom, 600)
                    """,
                    sid,
                )

                vul_names = [v["name"] for v in vul]

                near = (
                    f" Pontos sensíveis próximos: {', '.join(vul_names)}."
                    if vul
                    else ""
                )

                msg = (
                    f"UrbanPulse "
                    f"[{'CRÍTICO' if level == 'critical' else 'ATENÇÃO'}] "
                    f"{LABEL[hazard]} – "
                    f"risco de {risk:.0%} na próxima hora em {name}. "
                    f"{TIP[hazard]}{near}"
                )

                # Criação do alerta dentro da mesma transação protegida.
                row = await c.fetchrow(
                    """
                    INSERT INTO alerts(
                        sensor_id,
                        hazard,
                        risk,
                        level,
                        message,
                        features,
                        geom
                    )
                    SELECT
                        $1::text,
                        $2::text,
                        $3::real,
                        $4::text,
                        $5::text,
                        $6::jsonb,
                        geom
                    FROM sensors
                    WHERE id = $1::text
                    RETURNING
                        id,
                        ST_Y(geom::geometry) AS lat,
                        ST_X(geom::geometry) AS lon,
                        created_at
                    """,
                    sid,
                    hazard,
                    risk,
                    level,
                    msg,
                    feats,
                )

                targets = [
                    dict(t)
                    for t in await c.fetch(
                        TARGETS_SQL,
                        row["id"],
                        hazard,
                    )
                ]

    except asyncpg.UniqueViolationError:
        # Outra proteção: se uma restrição UNIQUE impedir a inserção,
        # o worker simplesmente não cria uma segunda ocorrência.
        return

    alert = {
        "id": row["id"],
        "hazard": hazard,
        "level": level,
        "risk": risk,
        "message": msg,
        "sensor_id": sid,
        "lat": row["lat"],
        "lon": row["lon"],
    }

    # Dispara as ações automáticas para os alvos encontrados.
    results = await asyncio.gather(
        *(dispatch(http, t, alert) for t in targets)
    )

    async with pool.acquire() as c:
        await c.executemany(
            """
            INSERT INTO actions(
                alert_id,
                target_id,
                kind,
                status,
                payload,
                response
            )
            VALUES($1, $2, $3, $4, $5, $6)
            """,
            [
                (
                    row["id"],
                    x["target_id"],
                    x["kind"],
                    x["status"],
                    x["payload"],
                    x["response"],
                )
                for x in results
            ],
        )

    names = {
        t["id"]: t["name"]
        for t in targets
    }

    acts = [
        {
            "kind": x["kind"],
            "target": names[x["target_id"]],
            "status": x["status"],
        }
        for x in results
    ]

    # Publica o alerta para os clientes conectados via SSE.
    await publish(
        r,
        {
            "event": "alert",
            "alert": {
                **{
                    k: alert[k]
                    for k in (
                        "id",
                        "hazard",
                        "level",
                        "risk",
                        "message",
                        "sensor_id",
                        "lat",
                        "lon",
                    )
                },
                "created_at": row["created_at"].isoformat(),
                "actions": acts,
            },
        },
    )

    print(
        f"[alert] #{row['id']} {hazard} {level} "
        f"risk={risk:.2f} sensor={sid} actions={len(results)}",
        flush=True,
    )

    ctx = {
        "sensor_name": name,
        "features": feats,
        "vulnerable": vul_names,
        "actions": acts,
    }

    # Executa o fluxo de resposta em segundo plano.
    task = asyncio.create_task(
        run_response(pool, r, http, alert, ctx)
    )

    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


async def handle(pool, r, http, fields):

    d = json.loads(fields["data"])

    sid = d["sensor_id"]
    ts = datetime.fromisoformat(d["ts"])

    async with pool.acquire() as c:

        await c.execute(
            """
            INSERT INTO readings(
                sensor_id,
                ts,
                pm25,
                temperature,
                humidity,
                water_level_pct,
                smoke_ppm,
                flame_detected
            )
            VALUES($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            sid,
            ts,
            d.get("pm25"),
            d.get("temperature"),
            d.get("humidity"),
            d.get("water_level_pct"),
            d.get("smoke_ppm"),
            d.get("flame_detected"),
        )

        await c.execute(
            "UPDATE sensors SET last_seen = now() WHERE id = $1",
            sid,
        )

        s = await c.fetchrow(
            """
            SELECT
                name,
                ST_Y(geom::geometry) lat,
                ST_X(geom::geometry) lon
            FROM sensors
            WHERE id = $1
            """,
            sid,
        )

        st = await c.fetchrow(STATE_SQL, sid)

    wx = await get_weather(
        r,
        http,
        s["lat"],
        s["lon"],
    )

    hour = datetime.now(
        ZoneInfo("America/Sao_Paulo")
    ).hour

    inputs = {}

    if st["water"] is not None:
        inputs["flood"] = {
            "water_level_pct": st["water"],
            "water_delta_15m": (
                st["water"] - (st["water_old"] or st["water"])
            ),
            "rain_last_1h_mm": wx["rain_last_1h_mm"],
            "rain_next_1h_mm": wx["rain_next_1h_mm"],
        }

    if st["temp"] is not None and st["hum"] is not None:
        inputs["heat"] = {
            "temperature": st["temp"],
            "humidity": st["hum"],
            "forecast_temp_3h": max(
                wx["temp_next_3h"] or st["temp"],
                st["temp"],
            ),
            "hour": hour,
        }

    if st["pm25"] is not None:
        inputs["air"] = {
            "pm25": st["pm25"],
            "pm25_delta_15m": (
                st["pm25"] - (st["pm25_old"] or st["pm25"])
            ),
            "traffic_index": traffic_index(),
            "wind_speed": wx["wind_speed"],
        }

    if st["smoke"] is not None:

        smoke = float(st["smoke"] or 0)
        flame = bool(st["flame"])

        # Regra demonstrativa:
        # fumaça crescente + chama confirmada elevam o risco.
        risk = (
            0.97
            if flame
            else min(
                0.95,
                max(0.0, (smoke - 35.0) / 250.0),
            )
        )

        feats = {
            "smoke_ppm": smoke,
            "flame_detected": flame,
            "temperature": float(st["temp"] or 25),
        }

        await evaluate(
            pool,
            r,
            http,
            sid,
            s["name"],
            "fire",
            risk,
            {
                **feats,
                "weather_source": wx["source"],
            },
        )

    for hazard, feats in inputs.items():

        risk = ml.predict(hazard, feats)

        await evaluate(
            pool,
            r,
            http,
            sid,
            s["name"],
            hazard,
            risk,
            {
                **feats,
                "weather_source": wx["source"],
            },
        )


async def main():

    pool = await create_pool()

    r = aioredis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=30,
        health_check_interval=15,
    )

    try:
        await r.xgroup_create(
            STREAM,
            GROUP,
            id="0",
            mkstream=True,
        )

    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    print(
        f"[worker] {CONSUMER} pronto",
        flush=True,
    )

    async with httpx.AsyncClient() as http:

        await recover(pool, r, http)

        while True:

            try:
                # Reprocessa mensagens pendentes de workers
                # que ficaram mais de 60 segundos sem ACK.
                _, claimed, _ = await r.xautoclaim(
                    STREAM,
                    GROUP,
                    CONSUMER,
                    min_idle_time=60_000,
                    count=20,
                )

                resp = await r.xreadgroup(
                    GROUP,
                    CONSUMER,
                    {STREAM: ">"},
                    count=50,
                    block=5000,
                )

            except (
                aioredis.ConnectionError,
                aioredis.TimeoutError,
            ) as exc:

                print(
                    f"[worker] redis indisponível ({exc!r}); "
                    "tentando de novo em 2s",
                    flush=True,
                )

                await asyncio.sleep(2)
                continue

            batch = (
                list(claimed)
                + [
                    m
                    for _, msgs in (resp or [])
                    for m in msgs
                ]
            )

            for msg_id, fields in batch:

                try:
                    await handle(
                        pool,
                        r,
                        http,
                        fields,
                    )

                except Exception as exc:

                    print(
                        f"[worker] erro em {msg_id}: {exc!r}",
                        flush=True,
                    )

                await r.xack(
                    STREAM,
                    GROUP,
                    msg_id,
                )


if __name__ == "__main__":
    asyncio.run(main())
