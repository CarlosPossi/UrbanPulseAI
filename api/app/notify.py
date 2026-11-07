"""Notificações no Telegram: mensagem rica (HTML + botões) que é ATUALIZADA a cada fase
da ocorrência (equipes acionadas -> no local -> solucionado) e avisa de novo ao concluir."""
import asyncio
import html
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import (PUBLIC_URL, TELEGRAM_API_BASE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
                     TELEGRAM_RESIDENT_BOT_TOKEN, TELEGRAM_RESIDENT_CHAT_ID)

SP = ZoneInfo("America/Sao_Paulo")
HAZ = {"flood": ("🌊", "Alagamento"), "heat": ("🔥", "Calor extremo"), "air": ("🌫", "Poluição do ar"), "fire": ("🔥", "Queimada / foco de incêndio")}
ACT = {"whatsapp": "💬 WhatsApp", "traffic_signal": "🚦 Semáforo", "irrigation": "💧 Irrigação"}
OK = {"sent": "✅", "failed": "❌", "simulated": "🔸"}
TITLE = {"dispatched": {"critical": "🚨 <b>OCORRÊNCIA CRÍTICA</b>", "warning": "⚠️ <b>ALERTA DE ATENÇÃO</b>"},
         "on_site": "🛠 <b>ATENDIMENTO NO LOCAL</b>", "resolved": "✅ <b>OCORRÊNCIA SOLUCIONADA</b>"}
e = html.escape


def chats() -> list[str]:
    return [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]


def configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and chats())


def hhmm(dt: datetime | None = None) -> str:
    return (dt or datetime.now(SP)).astimezone(SP).strftime("%H:%M")


def render(st: dict) -> tuple[str, list]:
    """Estado da ocorrência -> (texto HTML, teclado inline)."""
    a, emoji = st["alert"], HAZ[st["alert"]["hazard"]]
    title = TITLE[st["phase"]] if st["phase"] != "dispatched" else TITLE["dispatched"][a["level"]]
    filled = round(a["risk"] * 10)
    L = [title, "",
         f"{emoji[0]} <b>{emoji[1]}</b> · {e(a['sensor_name'])}",
         f"📊 Risco previsto: <b>{a['risk']:.0%}</b>  <code>{'█' * filled}{'░' * (10 - filled)}</code>",
         f"🕒 {e(st['opened'])} · Ocorrência #{a['id']}", "",
         "🧠 <b>Análise da IA</b>", e(st["plan"]["summary"]), f"<i>{e(st['plan']['advice'])}</i>"]
    if st["vulnerable"]:
        L += ["", "🏫 <b>Pontos sensíveis próximos</b>"] + [f"• {e(v)}" for v in st["vulnerable"]]
    if st["actions"]:
        L += ["", "⚙️ <b>Ações automáticas</b>"] + [f"{OK.get(x['status'], '•')} {ACT[x['kind']]} → {e(x['target'])}" for x in st["actions"]]
    if st["units"]:
        L += ["", "🚑 <b>Equipes em campo</b>"]
        for u in st["units"]:
            state = "✅ no local" if u["arrived"] else f"⏳ ETA {u['eta_min']} min"
            L.append(f"{u['emoji']} <b>{e(u['label'])}</b> · {e(u['base'].split(' – ', 1)[-1])} · {state}")
    if st["timeline"]:
        L += ["", "⏱ <b>Linha do tempo</b>"] + [f"<code>{t}</code> {e(x)}" for t, x in st["timeline"]]
    lat, lon = a["lat"], a["lon"]
    kb = [[{"text": "📍 Abrir no mapa", "url": f"https://www.google.com/maps?q={lat},{lon}"},
           {"text": "🧭 Rota até o local", "url": f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"}]]
    if PUBLIC_URL.startswith("https://") and "localhost" not in PUBLIC_URL:
        kb.append([{"text": "🖥 Abrir painel ao vivo", "url": PUBLIC_URL}])
    return "\n".join(L), kb


async def _call(http: httpx.AsyncClient, method: str, payload: dict):
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/{method}"
    for attempt in (1, 2):
        try:
            r = await http.post(url, json=payload, timeout=10)
            j = r.json()
            if r.status_code == 429 and attempt == 1:
                await asyncio.sleep(min(j.get("parameters", {}).get("retry_after", 1), 5))
                continue
            if j.get("ok"):
                return j["result"]
            if "not modified" not in str(j.get("description", "")):
                print(f"[telegram] {method}: {j.get('description')}", flush=True)
            return None
        except Exception as exc:
            print(f"[telegram] {method} falhou: {exc!r}", flush=True)
            return None


class IncidentNotifier:
    """Uma mensagem por ocorrência, editada a cada fase."""

    def __init__(self, http: httpx.AsyncClient, state: dict):
        self.http, self.state, self.ids = http, state, {}

    def event(self, text: str):
        self.state["timeline"].append((hhmm(), text))

    async def start(self):
        if not configured():
            print("[telegram] não configurado; notificação mantida apenas no painel.", flush=True)
            return
        text, kb = render(self.state)
        for chat in chats():
            res = await _call(self.http, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                         "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": kb}})
            if res:
                self.ids[chat] = res["message_id"]
                a = self.state["alert"]
                await _call(self.http, "sendLocation", {"chat_id": chat, "latitude": a["lat"], "longitude": a["lon"],
                                                        "reply_to_message_id": res["message_id"], "disable_notification": True})

    async def update(self):
        text, kb = render(self.state)
        for chat, mid in self.ids.items():
            await _call(self.http, "editMessageText", {"chat_id": chat, "message_id": mid, "text": text, "parse_mode": "HTML",
                                                       "disable_web_page_preview": True, "reply_markup": {"inline_keyboard": kb}})

    async def ping(self, text: str):
        """Mensagem nova (edições não geram notificação no celular)."""
        for chat, mid in self.ids.items():
            await _call(self.http, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                   "reply_to_message_id": mid})


async def send_test(http: httpx.AsyncClient) -> int:
    n = 0
    for chat in chats():
        if await _call(http, "sendMessage", {"chat_id": chat, "parse_mode": "HTML",
                                             "text": "✅ <b>UrbanPulse AI</b>\nTelegram configurado. Você receberá as ocorrências aqui."}):
            n += 1
    return n


def residents_configured() -> bool:
    return bool(TELEGRAM_RESIDENT_BOT_TOKEN and TELEGRAM_RESIDENT_CHAT_ID.strip())


async def _resident_send(http: httpx.AsyncClient, text: str):
    """Envia aviso público ao canal/grupo de moradores, separado do canal operacional."""
    if not residents_configured():
        return 0
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_RESIDENT_BOT_TOKEN}/sendMessage"
    sent = 0
    for chat in [x.strip() for x in TELEGRAM_RESIDENT_CHAT_ID.split(",") if x.strip()]:
        try:
            r = await http.post(url, json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                           "disable_web_page_preview": True}, timeout=10)
            if r.is_success and r.json().get("ok"):
                sent += 1
            else:
                print(f"[telegram moradores] envio falhou: {r.text[:200]}", flush=True)
        except Exception as exc:
            print(f"[telegram moradores] erro: {exc!r}", flush=True)
    return sent


def resident_open_text(alert: dict, plan: dict, features: dict) -> str:
    emoji, label = HAZ[alert["hazard"]]
    lines = [f"{emoji} <b>ALERTA À COMUNIDADE — {label.upper()}</b>",
             f"📍 Local monitorado: <b>{e(alert['sensor_name'])}</b>",
             f"⚠️ Nível: <b>{'CRÍTICO' if alert['level']=='critical' else 'ATENÇÃO'}</b> · risco estimado {alert['risk']:.0%}"]
    if alert["hazard"] == "fire":
        lines.append(f"🌫 Fumaça detectada: {float(features.get('smoke_ppm', 0)):.0f} ppm · chama: {'detectada' if features.get('flame_detected') else 'não confirmada'}")
    elif alert["hazard"] == "flood":
        lines.append(f"💧 Nível do bueiro monitorado: {float(features.get('water_level_pct', 0)):.0f}%")
    elif alert["hazard"] == "heat":
        lines.append(f"🌡 Temperatura: {float(features.get('temperature', 0)):.1f} °C · umidade: {float(features.get('humidity', 0)):.0f}%")
    elif alert["hazard"] == "air":
        lines.append(f"🌫 Material particulado PM2.5: {float(features.get('pm25', 0)):.0f} µg/m³")
    lines += ["", "<b>Como se proteger agora</b>", e(plan["advice"]),
              "Mantenha distância da área monitorada e acompanhe os avisos oficiais. Em risco imediato, acione os serviços de emergência (193 para Bombeiros)."]
    return "\n".join(lines)


def resident_resolved_text(alert: dict, plan: dict) -> str:
    emoji, label = HAZ[alert["hazard"]]
    lines = [f"✅ <b>OCORRÊNCIA CONTROLADA — {label.upper()}</b>",
             f"📍 Região: <b>{e(alert['sensor_name'])}</b>",
             "As equipes simuladas concluíram o atendimento e o UrbanPulse registrou a normalização do evento.",
             "", "<b>Cuidados recomendados nos próximos 7 dias</b>"]
    if alert["hazard"] == "fire":
        lines += ["• Evite entrar na área queimada ou tocar em cinzas e materiais remanescentes.",
                  "• Não acenda fogueiras, queime lixo ou vegetação; descarte bitucas corretamente.",
                  "• Mantenha portas e janelas fechadas se houver retorno de fumaça; reduza atividades ao ar livre quando o ar estiver ruim.",
                  "• Observe focos de fumaça, calor ou reignição e comunique imediatamente aos Bombeiros (193).",
                  "• Siga orientações da Defesa Civil e não bloqueie acessos usados pelas equipes."]
    elif alert["hazard"] == "flood":
        lines += ["• Evite áreas baixas e vias que possam voltar a alagar.", "• Não atravesse água acumulada a pé ou de veículo.",
                  "• Higienize objetos que tiveram contato com água de enchente e acompanhe avisos de chuva."]
    elif alert["hazard"] == "heat":
        lines += ["• Mantenha hidratação frequente e ambientes ventilados.", "• Evite esforço físico nos horários mais quentes.",
                  "• Redobre a atenção com crianças, idosos e animais."]
    else:
        lines += ["• Acompanhe os boletins de qualidade do ar.", "• Evite exercícios ao ar livre em períodos de fumaça ou poluição elevada.",
                  "• Mantenha ambientes internos ventilados quando a qualidade do ar externo estiver adequada."]
    lines += ["", "Este aviso é preventivo. Se as condições piorarem, afaste-se do local e procure os canais oficiais de emergência."]
    return "\n".join(lines)
