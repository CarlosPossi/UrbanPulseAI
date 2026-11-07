"""Citizen Action API: transforma um alerta em comandos reais via webhooks."""
import httpx
from .config import DRY_RUN, WEBHOOK_TOKEN


def build_payload(target: dict, alert: dict) -> dict:
    kind = target["kind"]
    base = {"alert_id": alert["id"], "hazard": alert["hazard"], "level": alert["level"],
            "risk": round(alert["risk"], 3)}
    if kind == "whatsapp":
        return {**base, "to": target["contact"], "message": alert["message"]}
    if kind == "traffic_signal":
        return {**base, "signal_id": target["contact"], "command": "DIVERT_TRAFFIC",
                "reason": "flash_flood_risk", "expires_in_min": 60}
    return {**base, "controller_id": target["contact"], "command": "COOL_ROOF",
            "duration_min": 40 if alert["level"] == "critical" else 20}


async def dispatch(http: httpx.AsyncClient, target: dict, alert: dict) -> dict:
    payload = build_payload(target, alert)
    if DRY_RUN:
        return {"target_id": target["id"], "kind": target["kind"], "status": "simulated",
                "payload": payload, "response": "dry-run"}
    headers = {"Authorization": f"Bearer {WEBHOOK_TOKEN}"} if WEBHOOK_TOKEN else {}
    try:
        resp = await http.post(target["endpoint"], json=payload, headers=headers, timeout=8)
        status = "sent" if resp.is_success else "failed"
        text = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # rede, DNS, timeout...
        status, text = "failed", f"{type(exc).__name__}: {exc}"[:200]
    return {"target_id": target["id"], "kind": target["kind"], "status": status,
            "payload": payload, "response": text}
