"""Modelos leves de risco (RandomForest) para alagamento, calor e poluição.

IMPORTANTE: o bootstrap treina com dados sintéticos gerados por regras físicas
simples + ruído, para a API funcionar desde o primeiro boot. Em produção,
reotreine com o histórico real (tabelas readings + alerts) - ver README.
"""
import os
import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier

from .config import MODEL_DIR

FEATURES = {
    "flood": ["water_level_pct", "water_delta_15m", "rain_last_1h_mm", "rain_next_1h_mm"],
    "heat": ["temperature", "humidity", "forecast_temp_3h", "hour"],
    "air": ["pm25", "pm25_delta_15m", "traffic_index", "wind_speed"],
}

_cache: dict = {}


def _synthetic(hazard: str, n: int = 8000, seed: int = 42):
    rng = np.random.default_rng(seed)
    if hazard == "flood":
        level = rng.uniform(0, 100, n)
        delta = rng.normal(3, 8, n)
        rain_l = rng.gamma(1.2, 3, n)
        rain_n = rng.gamma(1.2, 4, n)
        X = np.c_[level, delta, rain_l, rain_n]
        score = level + 1.5 * delta + 1.8 * rain_l + 2.5 * rain_n
        y = score + rng.normal(0, 6, n) >= 95
    elif hazard == "heat":
        temp = rng.uniform(18, 42, n)
        hum = rng.uniform(20, 95, n)
        fc = temp + rng.normal(1.5, 2, n)
        hour = rng.integers(0, 24, n)
        X = np.c_[temp, hum, fc, hour]
        score = 0.5 * temp + 0.5 * fc + 0.08 * np.maximum(hum - 40, 0)
        y = score + rng.normal(0, 1, n) >= 38
    else:  # air
        pm = rng.gamma(2, 12, n)
        delta = rng.normal(0, 6, n)
        traffic = rng.uniform(0, 1, n)
        wind = rng.gamma(2, 1.5, n)
        X = np.c_[pm, delta, traffic, wind]
        score = pm + delta + 15 * traffic * (wind < 2)
        y = score + rng.normal(0, 3, n) >= 50
    return X, y.astype(int)


def train_all() -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)
    for hazard in FEATURES:
        X, y = _synthetic(hazard)
        model = RandomForestClassifier(
            n_estimators=80, max_depth=8, min_samples_leaf=5, random_state=0, n_jobs=1
        ).fit(X, y)
        joblib.dump(model, os.path.join(MODEL_DIR, f"{hazard}.joblib"))
        print(f"[ml] {hazard}: treinado (positivos={y.mean():.1%})")


def _load(hazard: str):
    if hazard not in _cache:
        path = os.path.join(MODEL_DIR, f"{hazard}.joblib")
        if not os.path.exists(path):
            train_all()
        _cache[hazard] = joblib.load(path)
    return _cache[hazard]


def predict(hazard: str, feats: dict) -> float:
    """Probabilidade (0-1) de condição crítica na próxima ~1h."""
    x = [[float(feats[f]) for f in FEATURES[hazard]]]
    return float(_load(hazard).predict_proba(x)[0][1])


if __name__ == "__main__":
    train_all()
