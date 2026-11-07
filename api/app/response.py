"""Orquestra a resposta a uma ocorrência: IA analisa -> equipes são despachadas pela rota ->
chegam -> atendem -> ocorrência é solucionada. Tudo registrado, publicado (SSE) e notificado."""
import asyncio
import json
import math
import time
from datetime import datetime, timezone

from . import ai
from .config import ALERT_CHANNEL, DEMO_SPEEDUP
from .notify import IncidentNotifier, hhmm, _resident_send, resident_open_text, resident_resolved_text
from .routing import get_route

ACTIVE = ("analyzing", "dispatched", "on_site")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def publish(r, payload: dict):
    payload["server_time"] = _now_iso()
    await r.publish(ALERT_CHANNEL, json.dumps(payload))


async def log(pool, r, alert_id: int, kind: str, text: str):
    await pool.execute("INSERT INTO ai_log(alert_id,kind,text) VALUES($1,$2,$3)", alert_id, kind, text)
    await publish(r, {"event": "ai_log", "alert_id": alert_id, "line": {"kind": kind, "text": text, "ts": _now_iso()}})


async def build_units(pool, http, alert: dict, plan: dict) -> list[dict]:
    units = []
    for i, want in enumerate(plan["units"]):
        meta = ai.UNITS[want["type"]]
        base = await pool.fetchrow(
            "SELECT name, ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon FROM response_bases "
            "WHERE kind=$1 ORDER BY geom <-> ST_MakePoint($2,$3)::geography LIMIT 1",
            want["type"], alert["lon"], alert["lat"])
        if not base:
            continue
        route = await get_route(http, (base["lat"], base["lon"]), (alert["lat"], alert["lon"]),
                                meta["speed_kmh"], seed=alert["id"] * 10 + i)
        units.append({
            "id": f"{alert['id']}-{want['type']}", "type": want["type"], "label": meta["label"], "emoji": meta["emoji"],
            "reason": want["reason"], "base": {"name": base["name"], "lat": base["lat"], "lon": base["lon"]},
            "route": route["points"], "route_source": route["source"],
            "distance_km": round(route["distance_m"] / 1000, 1),
            "eta_min": max(1, math.ceil(route["duration_s"] / 60)),
            "demo_eta_s": round(min(45, max(12, route["duration_s"] / DEMO_SPEEDUP)), 1),   # tempo da animação
            "delay_s": round(i * 1.2, 1),                                                    # saídas escalonadas
            "arrived": False,
        })
    return units


async def finalize(pool, r, aid: int, notifier: IncidentNotifier | None, started: float | None):
    row = await pool.fetchrow(
        "SELECT a.sensor_id, a.hazard, s.name FROM alerts a JOIN sensors s ON s.id=a.sensor_id WHERE a.id=$1", aid)
    await pool.execute("UPDATE alerts SET resolved_at=now() WHERE id=$1 AND resolved_at IS NULL", aid)
    await pool.execute("UPDATE responses SET status='resolved', finished_at=now() WHERE alert_id=$1", aid)
    # evita reabrir a mesma ocorrência enquanto a telemetria do evento ainda decai
    await r.set(f"resolved-cooldown:{row['sensor_id']}:{row['hazard']}", "1", ex=300)
    took = f" em {int(time.monotonic() - started)} s de operação" if started else ""
    msg = f"Ocorrência solucionada em {row['name']}{took}. Equipes liberadas."
    await log(pool, r, aid, "success", msg)
    await publish(r, {"event": "resolved", "alert_id": aid, "sensor_id": row["sensor_id"], "hazard": row["hazard"], "message": msg})
    if notifier:
        await _resident_send(notifier.http, resident_resolved_text(notifier.state["alert"], notifier.state["plan"]))
        notifier.state["phase"] = "resolved"
        notifier.event("Ocorrência solucionada")
        await notifier.update()
        await notifier.ping(f"✅ <b>Solucionado</b> — {notifier.state['alert']['sensor_name']}\n"
                            f"Equipes liberadas às {hhmm()}. Ocorrência #{aid} encerrada.")


async def run_response(pool, r, http, alert: dict, ctx: dict):
    """alert: id, hazard, level, risk, lat, lon, sensor_id. ctx: sensor_name, features, vulnerable, actions."""
    aid = alert["id"]
    started = time.monotonic()
    try:
        plan = ai.plan(alert, ctx)
        await pool.execute("INSERT INTO responses(alert_id,status,plan) VALUES($1,'analyzing',$2)", aid, plan)
        units = await build_units(pool, http, alert, plan)

        notifier = IncidentNotifier(http, {
            "alert": {**{k: alert[k] for k in ("id", "hazard", "level", "risk", "lat", "lon")}, "sensor_name": ctx["sensor_name"]},
            "plan": plan, "vulnerable": ctx["vulnerable"], "actions": ctx["actions"], "phase": "dispatched",
            "opened": hhmm(), "timeline": [(hhmm(), "Alerta aberto pela rede de sensores")],
            "units": [{"emoji": u["emoji"], "label": u["label"], "base": u["base"]["name"], "eta_min": u["eta_min"], "arrived": False} for u in units]})
        notifier.event(f"IA acionou {len(units)} equipes")
        await notifier.start()   # notifica o canal operacional já na decisão
        await _resident_send(http, resident_open_text(notifier.state["alert"], plan, ctx["features"]))

        for line in ai.analysis_lines(alert, plan, ctx["sensor_name"]):
            await log(pool, r, aid, line["kind"], line["text"])
            await asyncio.sleep(1.1)

        t0, started_at = time.monotonic(), datetime.now(timezone.utc)
        await pool.execute("UPDATE responses SET status='dispatched', units=$2, started_at=$3 WHERE alert_id=$1", aid, units, started_at)
        await publish(r, {"event": "team_dispatch", "alert_id": aid,
                          "response": {"status": "dispatched", "started_at": started_at.isoformat(), "plan": plan, "units": units}})
        for u in units:
            await log(pool, r, aid, "dispatch", f"{u['label']} saiu de {u['base']['name']} · {u['distance_km']} km · ETA {u['eta_min']} min.")

        first = True
        for u, uu in sorted(zip(units, notifier.state["units"]), key=lambda p: p[0]["delay_s"] + p[0]["demo_eta_s"]):
            wait = t0 + u["delay_s"] + u["demo_eta_s"] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            u["arrived"] = uu["arrived"] = True
            await publish(r, {"event": "team_arrived", "alert_id": aid, "unit_id": u["id"], "label": u["label"]})
            await log(pool, r, aid, "arrival", f"{u['label']} chegou ao local.")
            notifier.event(f"{u['label']} chegou ao local")
            if first:
                first = False
                notifier.state["phase"] = "on_site"
                await pool.execute("UPDATE responses SET status='on_site', units=$2 WHERE alert_id=$1", aid, units)
                await publish(r, {"event": "team_status", "alert_id": aid, "status": "on_site"})
            await notifier.update()

        work = 12 + 3 * len(units)
        await log(pool, r, aid, "decision", "Todas as equipes no local. Atendimento em andamento…")
        await asyncio.sleep(work / 2)
        await log(pool, r, aid, "decision", "Situação controlada. Verificando telemetria para confirmar a normalização.")
        await asyncio.sleep(work / 2)
        await finalize(pool, r, aid, notifier, started)
    except Exception as exc:
        print(f"[response] erro na ocorrência {aid}: {exc!r}", flush=True)
        await pool.execute("UPDATE responses SET status='failed' WHERE alert_id=$1", aid)


async def recover(pool, r, http):
    """Após reinício do worker: encerra respostas que já deveriam ter terminado ou agenda o término."""
    rows = await pool.fetch("SELECT alert_id, status, units, started_at FROM responses WHERE status = ANY($1::text[])", list(ACTIVE))
    for row in rows:
        if row["status"] == "analyzing" or not row["started_at"]:
            await pool.execute("UPDATE responses SET status='failed' WHERE alert_id=$1", row["alert_id"])
            continue
        units = row["units"] or []
        total = max([u["delay_s"] + u["demo_eta_s"] for u in units] or [0]) + 12 + 3 * len(units)
        remaining = max(0.0, total - (datetime.now(timezone.utc) - row["started_at"]).total_seconds())

        async def later(aid=row["alert_id"], wait=remaining):
            await asyncio.sleep(wait)
            await finalize(pool, r, aid, None, None)
        asyncio.create_task(later())
