from datetime import datetime
from pydantic import BaseModel, Field, model_validator


class ReadingIn(BaseModel):
    """Telemetria de um nó IoT. Envie apenas os sensores que o nó possui."""
    sensor_id: str = Field(min_length=1, max_length=40, examples=["S-001"])
    ts: datetime | None = Field(None, description="ISO-8601; se omitido usa a hora do servidor")
    pm25: float | None = Field(None, ge=0, le=1000, description="µg/m³")
    temperature: float | None = Field(None, ge=-30, le=70, description="°C")
    humidity: float | None = Field(None, ge=0, le=100, description="% UR")
    water_level_pct: float | None = Field(None, ge=0, le=100,
                                          description="Enchimento do bueiro (0 = vazio, 100 = transbordando)")
    smoke_ppm: float | None = Field(None, ge=0, le=10000, description="Concentração simulada de fumaça (ppm)")
    flame_detected: bool | None = Field(None, description="Detecção digital de chama")

    @model_validator(mode="after")
    def at_least_one_metric(self):
        if all(v is None for v in (self.pm25, self.temperature, self.humidity, self.water_level_pct, self.smoke_ppm, self.flame_detected)):
            raise ValueError("Envie ao menos uma métrica (pm25, temperature, humidity, water_level_pct, smoke_ppm ou flame_detected).")
        return self


class Accepted(BaseModel):
    status: str = "queued"
    stream_id: str
