"""IA de resposta (baseada em regras + saída dos modelos de risco).

Decide, para cada alerta: classificação, prioridade, quais equipes acionar e o que
orientar à população. Determinística (mesmo alerta => mesma decisão) e totalmente
explicável: cada decisão traz as razões que a justificam.
"""
import random

from .config import ALERT_THRESHOLD

HAZ = {"flood": "alagamento", "heat": "calor extremo", "air": "poluição do ar", "fire": "queimada / foco de incêndio"}

UNITS = {
    "bombeiros":    {"label": "Bombeiros",               "emoji": "🚒", "speed_kmh": 45},
    "samu":         {"label": "SAMU",                    "emoji": "🚑", "speed_kmh": 50},
    "defesa_civil": {"label": "Defesa Civil",            "emoji": "🦺", "speed_kmh": 40},
    "cet":          {"label": "CET",                     "emoji": "🚦", "speed_kmh": 35},
    "zeladoria":    {"label": "Zeladoria",               "emoji": "🛠", "speed_kmh": 30},
    "ambiental":    {"label": "Monitoramento ambiental", "emoji": "🧪", "speed_kmh": 40},
}

ADVICE = {
    "flood": "Evite a via e áreas alagadas, não atravesse com veículo e suba objetos do térreo.",
    "heat": "Hidrate-se, mantenha crianças e idosos em locais frescos e evite esforço ao ar livre.",
    "air": "Evite exercício ao ar livre; pessoas com asma ou doença respiratória devem ficar em ambiente fechado.",
    "fire": "Afaste-se imediatamente da fumaça e das chamas. Não tente apagar o incêndio, não atravesse a área atingida e mantenha crianças, idosos e animais em local seguro. Em emergência, ligue 193.",
}


def _reasons(hz: str, f: dict) -> list[str]:
    if hz == "flood":
        out = [f"Bueiro com {f['water_level_pct']:.0f}% de enchimento, variação de {f['water_delta_15m']:+.0f} p.p. em 15 min."]
        if f.get("rain_next_1h_mm", 0) + f.get("rain_last_1h_mm", 0) > 0.1:
            out.append(f"Previsão de {f['rain_next_1h_mm']:.1f} mm na próxima hora ({f['rain_last_1h_mm']:.1f} mm na última).")
        else:
            out.append("Sem chuva prevista: a subida sugere obstrução ou acúmulo local no escoamento.")
        return out
    if hz == "heat":
        return [f"{f['temperature']:.1f} °C com {f['humidity']:.0f}% de umidade; previsão de {f['forecast_temp_3h']:.1f} °C nas próximas 3 h.",
                "Combinação de calor e umidade eleva o risco de estresse térmico."]
    if hz == "fire":
        return [f"Sensor registrou {f['smoke_ppm']:.0f} ppm de fumaça.",
                "Detector de chama: " + ("ATIVADO." if f.get("flame_detected") else "não ativado; monitoramento de fumaça mantido.")]
    return [f"PM2.5 em {f['pm25']:.0f} µg/m³ ({f['pm25_delta_15m']:+.0f} em 15 min).",
            f"Tráfego em {f['traffic_index']:.0%} e vento de {f['wind_speed']:.1f} m/s limitam a dispersão do particulado."]


def plan(alert: dict, ctx: dict) -> dict:
    """alert: id, hazard, level, risk. ctx: features, vulnerable (nomes), sensor_name."""
    hz, lvl, risk = alert["hazard"], alert["level"], alert["risk"]
    vul = ctx["vulnerable"]
    rnd = random.Random(alert["id"])
    critical = lvl == "critical"

    kinds = ["defesa_civil"]
    if hz == "flood":
        kinds += ["cet", "zeladoria"] + (["bombeiros"] if critical or vul else [])
    elif hz == "heat":
        kinds += ["zeladoria"] + (["samu"] if critical or vul else [])
    elif hz == "fire":
        kinds += ["bombeiros", "ambiental"] + (["samu"] if critical or vul else [])
    else:
        kinds += ["ambiental"] + (["samu"] if critical and vul else [])

    why = {
        "defesa_civil": "coordenar a comunidade e a evacuação preventiva",
        "cet": "desviar o tráfego e sinalizar a via",
        "zeladoria": "desobstruir a drenagem" if hz == "flood" else "resfriar o local (irrigação e sombreamento)",
        "bombeiros": "prontidão de resgate em área de risco" + (" próxima a pontos sensíveis" if vul else ""),
        "samu": "apoio médico a grupos vulneráveis próximos",
        "ambiental": "confirmar a medição e identificar a fonte do particulado ou fumaça",
    }
    priority = min(5, (4 if critical else 3) + (1 if vul else 0))
    reasons = _reasons(hz, ctx["features"])
    reasons.append(f"Risco previsto pelo modelo: {risk:.0%} (limiar de alerta {ALERT_THRESHOLD:.0%}).")
    if vul:
        reasons.append(f"{len(vul)} ponto(s) sensível(is) a 600 m: {', '.join(vul)}.")
    classification = f"{HAZ[hz].capitalize()} {'crítico' if critical else 'em evolução'}"
    return {
        "classification": classification,
        "priority": priority,
        "confidence": round(min(0.99, 0.72 + risk * 0.25 + rnd.uniform(-0.02, 0.02)), 2),
        "reasons": reasons,
        "advice": ADVICE[hz],
        "units": [{"type": k, "reason": why[k]} for k in kinds],
        "summary": (f"{classification} em {ctx['sensor_name']}. Prioridade {priority}/5. "
                    f"Acionando {len(kinds)} equipes: {', '.join(UNITS[k]['label'] for k in kinds)}."),
    }


def analysis_lines(alert: dict, p: dict, sensor_name: str) -> list[dict]:
    lines = [{"kind": "analysis", "text": f"Recebi o alerta #{alert['id']} de {sensor_name}. Cruzando telemetria, previsão do tempo e pontos sensíveis…"}]
    lines += [{"kind": "analysis", "text": r} for r in p["reasons"][:4]]
    lines.append({"kind": "decision", "text": f"Classificação: {p['classification']}. Prioridade {p['priority']}/5 · confiança {p['confidence']:.0%}."})
    lines.append({"kind": "decision", "text": "Decisão: acionar " + ", ".join(UNITS[u["type"]]["label"] for u in p["units"]) + "."})
    return lines
