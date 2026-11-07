"""Rotas das equipes: OSRM (ruas reais) com fallback sintético em malha de ruas."""
import math
import random

import httpx

from .config import OSRM_URL


def haversine_m(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _downsample(pts, n=140):
    if len(pts) <= n:
        return pts
    step = (len(pts) - 1) / (n - 1)
    return [pts[round(i * step)] for i in range(n)]


def _synthetic(a, b, seed):
    """Percurso em 'escada' (ruas ortogonais) com curvas suaves, estável por ocorrência."""
    rnd = random.Random(seed)
    pts, cur, turns = [a], list(a), rnd.randint(3, 5)
    horizontal = rnd.random() < 0.5
    for i in range(turns):
        frac = (i + 1) / turns
        tgt_la = a[0] + (b[0] - a[0]) * frac + (rnd.uniform(-1, 1) * 0.002 if i < turns - 1 else 0)
        tgt_lo = a[1] + (b[1] - a[1]) * frac + (rnd.uniform(-1, 1) * 0.002 if i < turns - 1 else 0)
        for tgt in ((cur[0], tgt_lo), (tgt_la, tgt_lo)) if horizontal else ((tgt_la, cur[1]), (tgt_la, tgt_lo)):
            for k in range(1, 9):
                pts.append((cur[0] + (tgt[0] - cur[0]) * k / 8, cur[1] + (tgt[1] - cur[1]) * k / 8))
            cur = list(tgt)
    pts.append(b)
    return pts


async def get_route(http: httpx.AsyncClient, a, b, speed_kmh: float, seed: int) -> dict:
    """a, b = (lat, lon). Retorna pontos [[lat, lon]], distância (m), duração real (s) e a fonte."""
    try:
        url = f"{OSRM_URL}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}"
        r = await http.get(url, params={"overview": "full", "geometries": "geojson"}, timeout=3)
        r.raise_for_status()
        route = r.json()["routes"][0]
        pts = _downsample([(lat, lon) for lon, lat in route["geometry"]["coordinates"]])
        return {"points": [list(p) for p in pts], "distance_m": route["distance"],
                "duration_s": route["duration"] * 1.25, "source": "osrm"}  # +25%: urgência vs. trânsito
    except Exception:
        pts = _synthetic(a, b, seed)
        dist = sum(haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        return {"points": [list(p) for p in pts], "distance_m": dist,
                "duration_s": dist / (speed_kmh / 3.6), "source": "synthetic"}
