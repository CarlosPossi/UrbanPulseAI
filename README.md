# UrbanPulse AI
**Rede descentralizada de micro-mitigação climática e resiliência urbana** – API-first.

Sensores IoT de baixo custo (ESP32) → fila assíncrona → IA preditiva → **ações reais** (WhatsApp/SMS, semáforos, irrigação) → mapa em tempo real.

```
 ESP32 / cURL ──POST /v1/readings──▶  API (FastAPI)  ──XADD──▶  Redis Streams
                                        │  auth + validação          │ consumer group
                                        │  rate limit (Redis)        ▼
   Mapa (Leaflet) ◀── SSE /v1/stream ◀──┘◀── pub/sub ◀──────  Worker(s) escaláveis
                                                              1. grava leitura (PostGIS)
   PostgreSQL + PostGIS ◀─────────────────────────────────────2. features + clima (Open-Meteo)
   (sensores, leituras, alvos,                                3. RandomForest → risco 0-1
    alertas, ações, pontos vulneráveis)                       4. ST_DWithin → alvos no raio
                                                              5. webhooks → WhatsApp/semáforo/irrigação
```

## Rodar
```bash
docker compose up --build            # API + painel :8000, PostGIS, Redis, worker e gateway de nós
docker compose up --scale worker=3   # mais workers = mais vazão
```
- Painel: http://localhost:8000  · Swagger: http://localhost:8000/docs
- 14 sensores em São Paulo (`S-001..S-010` para riscos urbanos e `F-001..F-004` para queimadas), chave `dev-key-<ID>` (troque em produção).
- O serviço `nodes` (`api/app/emulator.py`) envia a telemetria da rede pela API pública, pelo mesmo caminho dos ESP32
  (`POST /v1/readings` + `X-Device-Key`), com ciclo diário e eventos climáticos espontâneos (chuva forte, calor, particulado e focos de queimada).
  Para usar hardware real, desligue o serviço `nodes` e grave `firmware/esp32_urbanpulse.ino` nos dispositivos.

## Demonstração
O painel já abre com histórico e a rede viva. Para forçar um cenário durante o pitch:
```bash
A='X-Admin-Key: troque-esta-chave'
curl -XPOST "localhost:8000/v1/admin/scenario?sensor_id=S-001&kind=flood" -H "$A"   # bueiro enchendo
curl -XPOST "localhost:8000/v1/admin/scenario?sensor_id=S-003&kind=heat"  -H "$A"   # onda de calor
curl -XPOST "localhost:8000/v1/admin/scenario?sensor_id=S-004&kind=air"   -H "$A"
curl -XPOST "localhost:8000/v1/admin/scenario?sensor_id=F-001&kind=fire" -H "$A"   # foco de queimada
curl -XPOST "localhost:8000/v1/admin/scenario?sensor_id=S-001&kind=clear" -H "$A"   # encerra o evento
```
Em cerca de 30–60 s o mapa dispara o aviso, acende o sensor e desenha as ações (WhatsApp, semáforo, irrigação).
Você também pode enviar leituras próprias com `curl` ou `simulator/simulate.py`. Para testar fumaça e chama diretamente: `python simulator/simulate.py --scenario fire --sensor F-001 --count 12 --interval 2`.


## IA de resposta, equipes em campo e Telegram
Cada alerta passa por uma **IA de resposta** (`api/app/ai.py`, baseada em regras + saída dos modelos de risco) que:
1. classifica a ocorrência e define a prioridade (1–5) com as razões (nível do bueiro, chuva, pontos sensíveis…);
2. decide **quais equipes acionar** (Defesa Civil, CET, Zeladoria, Bombeiros, SAMU, monitoramento ambiental);
3. despacha cada equipe da **base mais próxima** (PostGIS `<->`) pela rota até o local (OSRM; sem internet usa rota sintética);
4. acompanha chegada, atendimento e encerramento; tudo vai para o diário `ai_log` e para o painel em tempo real.

No mapa, cada equipe é um ícone que **percorre a rota** até a ocorrência (com rastro e contagem de ETA); ao recarregar a página a
animação retoma do ponto correto. O tempo é acelerado (`DEMO_SPEEDUP`) para caber na demonstração.

**Botão "Simular ocorrência"** (canto do mapa): escolha o tipo e o local (ou aleatório). Ele injeta leituras críticas pelo mesmo pipeline
dos sensores (`POST /v1/demo/incident`); desative com `DEMO_CONTROLS=false`.

**Telegram operacional**: preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no `.env` (copie de `.env.example`). Para avisos à comunidade, configure também `TELEGRAM_RESIDENT_CHAT_ID` (e, opcionalmente, `TELEGRAM_RESIDENT_BOT_TOKEN`; se vazio, reutiliza o bot operacional). A mensagem operacional traz risco, análise da IA,
pontos sensíveis, ações automáticas, equipes com ETA e linha do tempo, com botões *Abrir no mapa* e *Rota*. **A mesma mensagem é editada**
a cada fase (acionadas → no local → solucionado) e um aviso novo chega ao concluir. Teste com:
`curl -XPOST localhost:8000/v1/notify/test -H "X-Admin-Key: <ADMIN_KEY>"`.

> **Preservação dos dados:** evite `docker compose down -v`, pois o parâmetro `-v` apaga os volumes do PostgreSQL e do Redis, incluindo o histórico de leituras dos sensores. Para reiniciar os serviços sem apagar os dados, use `docker compose down` e depois `docker compose up --build`. O gateway agora verifica se o banco ainda tem leituras e repõe o histórico demonstrativo caso o volume do PostgreSQL tenha sido recriado.

## API
| Método | Rota | Função |
|---|---|---|
| POST | `/v1/readings` | Ingestão IoT (header `X-Device-Key`) → 202 + fila |
| GET | `/v1/sensors` | Status da rede + última leitura |
| GET | `/v1/sensors/{id}/readings` | Histórico |
| POST | `/v1/demo/incident` | Simula ocorrência (botão do painel) |
| GET | `/v1/ai/feed` · `/v1/ai/summary` · `/v1/bases` | Diário da IA, resumo e bases de resposta |
| POST | `/v1/notify/test` | Testa o Telegram (admin) |
| GET | `/v1/stats` · `/v1/targets` · `/v1/vulnerable` | Indicadores, atuadores e pontos sensíveis (painel) |
| GET | `/v1/alerts` | Alertas e ações disparadas |
| POST | `/v1/alerts/{id}/resolve` | Encerra alerta (header `X-Admin-Key`) |
| GET | `/v1/stream` | SSE em tempo real |
| GET | `/health` | Saúde + profundidade da fila |

### Monitoramento de queimadas
Quatro nós simulados adicionais (`F-001..F-004`) representam áreas verdes e de mata. Eles enviam temperatura, umidade, concentração de fumaça (`smoke_ppm`) e detecção de chama (`flame_detected`). O worker avalia a combinação de fumaça/chama e abre um alerta `fire`; a IA de resposta prioriza Bombeiros, Defesa Civil e monitoramento ambiental, com SAMU quando o risco ou a proximidade de pontos sensíveis justificar. O deslocamento e atendimento continuam no mesmo fluxo simulado das demais ocorrências. Para testar: `curl -XPOST "localhost:8000/v1/admin/scenario?sensor_id=F-001&kind=fire" -H "$A"` ou use o botão **Simular ocorrência → Queimada** no painel.

### Canal Telegram para moradores
Configure `TELEGRAM_RESIDENT_CHAT_ID` com o ID do canal/grupo comunitário e adicione o bot como administrador com permissão para publicar. O canal operacional continua separado. Moradores recebem o local e as condições detectadas, orientações preventivas e, ao final, confirmação de controle e cuidados recomendados pelos próximos 7 dias. Para a demonstração, o sistema usa textos preventivos por tipo de ocorrência; os avisos não substituem alertas oficiais nem confirmação de órgãos públicos.

## Decisões de engenharia
- **Ingestão desacoplada**: a API só valida, autentica e enfileira (202). IA e webhooks (lentos) rodam nos workers.
- **Redis Streams + consumer group**: escala horizontal, ACK por mensagem, reprocessamento de pendentes (`XAUTOCLAIM`).
- **PostGIS**: `ST_DWithin` escolhe só os destinatários/atuadores no raio do alerta; pontos vulneráveis (escolas, asilos) entram na mensagem.
- **Sem alerta duplicado**: índice único parcial (1 alerta ativo por sensor+risco) + histerese (abre ≥ 0,60; fecha < 0,30).
- **Resiliência**: webhook que falha fica registrado como `failed`; clima indisponível cai em fallback.

## Conectar serviços reais
Cada linha de `action_targets` tem um `endpoint`. Em dev apontam para `/mock/*`. Para produção, troque por:
- **WhatsApp/SMS**: um gateway que aceite JSON (Z-API, Zenvia, Twilio via função intermediária). O payload traz `to` e `message`.
- **Semáforos**: o endpoint da CET/prefeitura (payload `DIVERT_TRAFFIC`, `signal_id`) – exige convênio.
- **Irrigação**: controlador municipal (payload `COOL_ROOF`, `controller_id`, `duration_min`).
Use `WEBHOOK_TOKEN` (Bearer) e `DRY_RUN=true` para ensaiar sem disparar nada.

## Limitações (seja transparente com a banca)
- Os modelos de bootstrap são treinados com **dados sintéticos** (regras físicas + ruído). Para uso real, reotreine com `readings` + ocorrências reais (Defesa Civil/CGE) e valide recall/precisão.
- O índice de tráfego é um **proxy por horário**; troque por TomTom/Waze/CET em `weather.py`.
- Integrações de semáforo/WhatsApp estão **simuladas** por mocks até haver credenciais/convênio.
- A telemetria do repositório vem do gateway de nós (`emulator.py`); o firmware ESP32 **não foi testado em hardware** – calibre `DEPTH_CM` e pinos antes de usar.
- `readings` é tabela simples; em volume alto use particionamento ou TimescaleDB.

## Deploy gratuito
Render/Railway: serviços *web* (`api/Dockerfile`) e *worker* (`python -m app.worker`) + Postgres com PostGIS + Redis gerenciado. Aplique `db/init.sql` e configure as variáveis de `.env.example`.

## Roadmap
Retreino automático (job agendado) · app/bot para cidadãos reportarem · autenticação por JWT para operadores · métricas Prometheus · testes automatizados (pytest + testcontainers).


## IA simulada, equipes e notificações no celular
- O painel consulta `GET /v1/ai/summary` e apresenta um resumo heurístico das áreas com alertas ativos. É uma demonstração de IA baseada nos dados, não um LLM externo.
- Ao abrir uma ocorrência, o sistema simula uma equipe saindo da Base Operacional Centro, chegando ao sensor e realizando o reparo. O alerta é encerrado após a sequência demonstrativa (aprox. 38 segundos).
- Para receber mensagens reais no celular, configure um bot do Telegram:
  1. No Telegram, converse com `@BotFather`, crie um bot com `/newbot` e copie o token.
  2. Abra o bot criado e envie `/start`.
  3. Obtenha seu `chat_id` usando `https://api.telegram.org/botSEU_TOKEN/getUpdates` depois de mandar uma mensagem ao bot.
  4. Preencha `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` no arquivo `.env.example` (é o arquivo carregado pelo `docker-compose.yml` deste projeto).
  5. Reinicie: `docker compose up --build`.
- O Telegram operacional envia uma notificação quando a ocorrência é aberta e outra quando a equipe conclui o atendimento. O canal separado de moradores recebe um alerta preventivo por região e, ao encerrar, uma confirmação de controle com recomendações para os 7 dias seguintes. Sem token/chat_id, os eventos continuam funcionando no painel, mas nenhuma mensagem externa é enviada. Para o canal de moradores, o bot precisa ser adicionado ao canal/grupo e receber permissão para publicar.
- A sequência de equipe é simulada; não representa despacho de equipes reais nem integração com órgãos públicos.

- A IA de resposta é **baseada em regras** (explicável), não um LLM; o deslocamento das equipes é **simulado** (tempo acelerado), e as bases são fictícias.
