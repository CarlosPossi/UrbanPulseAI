import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://urbanpulse:urbanpulse@db:5432/urbanpulse")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ADMIN_KEY = os.getenv("ADMIN_KEY", "dev-admin")
MODEL_DIR = os.getenv("MODEL_DIR", "/app/models")

# DRY_RUN=true: não chama webhooks; registra as ações como "simulated"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")

ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "0.6"))      # abre alerta
CRITICAL_THRESHOLD = float(os.getenv("CRITICAL_THRESHOLD", "0.85"))
RESOLVE_THRESHOLD = float(os.getenv("RESOLVE_THRESHOLD", "0.3"))  # fecha alerta (histerese)

RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))  # por sensor

STREAM = "readings"
GROUP = "workers"
ALERT_CHANNEL = "alerts"

# Nós de borda (gateway de telemetria)
API_URL = os.getenv("API_URL", "http://localhost:8000")
NODE_TICK_SECONDS = float(os.getenv("NODE_TICK_SECONDS", "10"))
EVENT_EVERY_SECONDS = float(os.getenv("EVENT_EVERY_SECONDS", "240"))  # intervalo médio entre eventos climáticos

# Notificações reais via Telegram (preencha no .env para ativar)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
# Canal separado para moradores; usa o mesmo bot por padrão e outro chat/canal.
TELEGRAM_RESIDENT_BOT_TOKEN = os.getenv("TELEGRAM_RESIDENT_BOT_TOKEN", "") or TELEGRAM_BOT_TOKEN
TELEGRAM_RESIDENT_CHAT_ID = os.getenv("TELEGRAM_RESIDENT_CHAT_ID", "")

TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")                    # URL https pública do painel (botão no Telegram)
DEMO_CONTROLS = os.getenv("DEMO_CONTROLS", "true").lower() == "true"   # botão "Simular ocorrência"

# Resposta de campo
OSRM_URL = os.getenv("OSRM_URL", "https://router.project-osrm.org")   # rotas reais (opcional; há fallback)
DEMO_SPEEDUP = float(os.getenv("DEMO_SPEEDUP", "30"))      # 1 min real = 2 s na animação
