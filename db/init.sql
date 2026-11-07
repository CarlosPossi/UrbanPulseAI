CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE sensors (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  api_key_hash  TEXT NOT NULL,
  geom          GEOGRAPHY(Point, 4326) NOT NULL,
  last_seen     TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sensors_geom_gix ON sensors USING GIST (geom);

CREATE TABLE readings (
  id               BIGSERIAL PRIMARY KEY,
  sensor_id        TEXT NOT NULL REFERENCES sensors(id),
  ts               TIMESTAMPTZ NOT NULL,
  pm25             REAL,
  temperature      REAL,
  humidity         REAL,
  water_level_pct  REAL,
  smoke_ppm        REAL,
  flame_detected   BOOLEAN
);
CREATE INDEX readings_sensor_ts_idx ON readings (sensor_id, ts DESC);

-- escolas, asilos, UBS... usados para contextualizar alertas
CREATE TABLE vulnerable_points (
  id    SERIAL PRIMARY KEY,
  name  TEXT NOT NULL,
  kind  TEXT NOT NULL,
  geom  GEOGRAPHY(Point, 4326) NOT NULL
);
CREATE INDEX vulnerable_geom_gix ON vulnerable_points USING GIST (geom);

-- destinatários/atuadores: WhatsApp, semáforo, irrigação
CREATE TABLE action_targets (
  id        SERIAL PRIMARY KEY,
  kind      TEXT NOT NULL CHECK (kind IN ('whatsapp','traffic_signal','irrigation')),
  name      TEXT NOT NULL,
  contact   TEXT NOT NULL,           -- telefone ou ID do equipamento
  endpoint  TEXT NOT NULL,           -- webhook que recebe o comando
  hazards   TEXT[] NOT NULL,         -- flood | heat | air | fire
  radius_m  INT NOT NULL DEFAULT 2000,
  active    BOOLEAN NOT NULL DEFAULT TRUE,
  geom      GEOGRAPHY(Point, 4326) NOT NULL
);
CREATE INDEX targets_geom_gix ON action_targets USING GIST (geom);

CREATE TABLE alerts (
  id           BIGSERIAL PRIMARY KEY,
  sensor_id    TEXT NOT NULL REFERENCES sensors(id),
  hazard       TEXT NOT NULL,
  risk         REAL NOT NULL,
  level        TEXT NOT NULL,
  message      TEXT NOT NULL,
  features     JSONB NOT NULL,
  geom         GEOGRAPHY(Point, 4326) NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at  TIMESTAMPTZ
);
-- 1 alerta ativo por sensor/risco, mesmo com vários workers concorrentes
CREATE UNIQUE INDEX alerts_one_active ON alerts (sensor_id, hazard) WHERE resolved_at IS NULL;

CREATE TABLE actions (
  id         BIGSERIAL PRIMARY KEY,
  alert_id   BIGINT NOT NULL REFERENCES alerts(id),
  target_id  INT NOT NULL REFERENCES action_targets(id),
  kind       TEXT NOT NULL,
  status     TEXT NOT NULL,          -- sent | failed | simulated
  payload    JSONB NOT NULL,
  response   TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX actions_alert_idx ON actions (alert_id);

-- bases das equipes de resposta (bombeiros, SAMU, Defesa Civil, CET, zeladoria, ambiental)
CREATE TABLE response_bases (
  id    SERIAL PRIMARY KEY,
  name  TEXT NOT NULL,
  kind  TEXT NOT NULL CHECK (kind IN ('bombeiros','samu','defesa_civil','cet','zeladoria','ambiental')),
  geom  GEOGRAPHY(Point, 4326) NOT NULL
);
CREATE INDEX bases_geom_gix ON response_bases USING GIST (geom);

-- plano e andamento da resposta a cada alerta (decidido pela IA de resposta)
CREATE TABLE responses (
  alert_id     BIGINT PRIMARY KEY REFERENCES alerts(id),
  status       TEXT NOT NULL,            -- analyzing | dispatched | on_site | resolved | failed
  plan         JSONB NOT NULL,
  units        JSONB NOT NULL DEFAULT '[]',
  started_at   TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ
);

-- diário de decisões da IA (alimenta o copiloto do painel)
CREATE TABLE ai_log (
  id        BIGSERIAL PRIMARY KEY,
  alert_id  BIGINT REFERENCES alerts(id),
  ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind      TEXT NOT NULL,               -- analysis | decision | dispatch | arrival | success
  text      TEXT NOT NULL
);
CREATE INDEX ai_log_ts_idx ON ai_log (ts DESC);


-- ---------- Seed de demonstração (São Paulo) ----------
-- chave de cada sensor: dev-key-<ID>  (ex.: dev-key-S-001). Troque em produção!
INSERT INTO sensors (id, name, api_key_hash, geom) VALUES
 ('S-001','Pinheiros – Rua Butantã', encode(sha256('dev-key-S-001'::bytea),'hex'), ST_MakePoint(-46.699,-23.567)::geography),
 ('S-002','Itaquera – Av. Radial Leste', encode(sha256('dev-key-S-002'::bytea),'hex'), ST_MakePoint(-46.457,-23.54)::geography),
 ('S-003','Paraisópolis – R. Silveira Sampaio', encode(sha256('dev-key-S-003'::bytea),'hex'), ST_MakePoint(-46.7283,-23.6226)::geography),
 ('S-004','Brás – Rua Oriente', encode(sha256('dev-key-S-004'::bytea),'hex'), ST_MakePoint(-46.615,-23.545)::geography),
 ('S-005','Capão Redondo – Estrada de Itapecerica', encode(sha256('dev-key-S-005'::bytea),'hex'), ST_MakePoint(-46.78,-23.66)::geography),
 ('S-006','Brasilândia – Av. Elísio Teixeira Leite', encode(sha256('dev-key-S-006'::bytea),'hex'), ST_MakePoint(-46.693,-23.456)::geography),
 ('S-007','Cidade Tiradentes – Av. dos Metalúrgicos', encode(sha256('dev-key-S-007'::bytea),'hex'), ST_MakePoint(-46.409,-23.589)::geography),
 ('S-008','Heliópolis – Estrada das Lágrimas', encode(sha256('dev-key-S-008'::bytea),'hex'), ST_MakePoint(-46.61,-23.6)::geography),
 ('S-009','Jardim Ângela – Estrada do M''Boi Mirim', encode(sha256('dev-key-S-009'::bytea),'hex'), ST_MakePoint(-46.77,-23.7)::geography),
 ('S-010','Penha – Av. Amador Bueno da Veiga', encode(sha256('dev-key-S-010'::bytea),'hex'), ST_MakePoint(-46.542,-23.526)::geography);
INSERT INTO sensors (id, name, api_key_hash, geom) VALUES
 ('F-001','Cantareira – Núcleo de mata', encode(sha256('dev-key-F-001'::bytea),'hex'), ST_MakePoint(-46.638,-23.395)::geography),
 ('F-002','Parque do Carmo – Área verde', encode(sha256('dev-key-F-002'::bytea),'hex'), ST_MakePoint(-46.472,-23.578)::geography),
 ('F-003','Horto Florestal – Zona norte', encode(sha256('dev-key-F-003'::bytea),'hex'), ST_MakePoint(-46.633,-23.466)::geography),
 ('F-004','APA Capivari-Monos – Zona sul', encode(sha256('dev-key-F-004'::bytea),'hex'), ST_MakePoint(-46.700,-23.860)::geography);
INSERT INTO vulnerable_points (name, kind, geom) VALUES
 ('EMEF Pinheiros','escola', ST_MakePoint(-46.7005,-23.5675)::geography),
 ('Lar dos Idosos Itaquera','asilo', ST_MakePoint(-46.458,-23.541)::geography),
 ('EMEI Paraisópolis','escola', ST_MakePoint(-46.729,-23.623)::geography),
 ('UBS Brás','saude', ST_MakePoint(-46.6155,-23.5455)::geography),
 ('EMEF Capão Redondo','escola', ST_MakePoint(-46.779,-23.6605)::geography),
 ('Lar Vila Brasilândia','asilo', ST_MakePoint(-46.6925,-23.4565)::geography),
 ('CEU Cidade Tiradentes','escola', ST_MakePoint(-46.41,-23.5895)::geography),
 ('UBS Heliópolis','saude', ST_MakePoint(-46.6105,-23.6005)::geography),
 ('EMEI Jardim Ângela','escola', ST_MakePoint(-46.771,-23.7005)::geography),
 ('Asilo Santa Penha','asilo', ST_MakePoint(-46.543,-23.5265)::geography);
-- Em dev, os webhooks apontam para os endpoints internos da própria API (http://api:8000/mock/...).
INSERT INTO action_targets (kind,name,contact,endpoint,hazards,radius_m,geom) VALUES
 ('whatsapp','Defesa Civil – Regional Pinheiros','+5511900000001','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.698,-23.5662)::geography),
 ('whatsapp','Líder comunitário – Itaquera','+5511900000002','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.456,-23.539199999999997)::geography),
 ('whatsapp','Líder comunitária – Paraisópolis','+5511900000003','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.7273,-23.621799999999997)::geography),
 ('whatsapp','Defesa Civil – Regional Sé/Brás','+5511900000004','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.614000000000004,-23.5442)::geography),
 ('whatsapp','Líder comunitário – Capão Redondo','+5511900000005','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.779,-23.6592)::geography),
 ('whatsapp','Defesa Civil – Regional Freguesia/Brasilândia','+5511900000006','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.692,-23.455199999999998)::geography),
 ('whatsapp','Líder comunitária – Cidade Tiradentes','+5511900000007','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.408,-23.588199999999997)::geography),
 ('whatsapp','Líder comunitário – Heliópolis','+5511900000008','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.609,-23.5992)::geography),
 ('whatsapp','Defesa Civil – Regional M''Boi Mirim','+5511900000009','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.769000000000005,-23.699199999999998)::geography),
 ('whatsapp','Defesa Civil – Regional Penha','+5511900000010','http://api:8000/mock/whatsapp','{flood,heat,air,fire}',2500, ST_MakePoint(-46.541000000000004,-23.525199999999998)::geography),
 ('traffic_signal','Semáforo SEM-114 (Av. Butantã)','SEM-114','http://api:8000/mock/traffic','{flood}',1500, ST_MakePoint(-46.6985,-23.5665)::geography),
 ('traffic_signal','Semáforo SEM-207 (Radial Leste)','SEM-207','http://api:8000/mock/traffic','{flood}',1500, ST_MakePoint(-46.4565,-23.5395)::geography),
 ('traffic_signal','Semáforo SEM-322 (Brás)','SEM-322','http://api:8000/mock/traffic','{flood}',1500, ST_MakePoint(-46.6145,-23.5448)::geography),
 ('traffic_signal','Semáforo SEM-418 (Heliópolis)','SEM-418','http://api:8000/mock/traffic','{flood}',1500, ST_MakePoint(-46.6095,-23.5995)::geography),
 ('traffic_signal','Semáforo SEM-509 (Penha)','SEM-509','http://api:8000/mock/traffic','{flood}',1500, ST_MakePoint(-46.5415,-23.5255)::geography),
 ('traffic_signal','Semáforo SEM-611 (Brasilândia)','SEM-611','http://api:8000/mock/traffic','{flood}',1500, ST_MakePoint(-46.6935,-23.4555)::geography),
 ('irrigation','Telhado verde – Pinheiros','IRR-018','http://api:8000/mock/irrigation','{heat}',2000, ST_MakePoint(-46.6995,-23.5668)::geography),
 ('irrigation','Telhado verde – CEU Paraisópolis','IRR-031','http://api:8000/mock/irrigation','{heat}',2000, ST_MakePoint(-46.728,-23.6224)::geography),
 ('irrigation','Horta urbana – Itaquera','IRR-052','http://api:8000/mock/irrigation','{heat}',2000, ST_MakePoint(-46.4575,-23.5405)::geography),
 ('irrigation','Horta comunitária – Capão Redondo','IRR-064','http://api:8000/mock/irrigation','{heat}',2000, ST_MakePoint(-46.7795,-23.6595)::geography),
 ('irrigation','Telhado verde – Cidade Tiradentes','IRR-077','http://api:8000/mock/irrigation','{heat}',2000, ST_MakePoint(-46.4095,-23.5885)::geography),
 ('irrigation','Horta urbana – Jardim Ângela','IRR-090','http://api:8000/mock/irrigation','{heat}',2000, ST_MakePoint(-46.7705,-23.6995)::geography);

INSERT INTO response_bases (name, kind, geom) VALUES
 ('Bombeiros – Posto Centro','bombeiros', ST_MakePoint(-46.6388,-23.5489)::geography),
 ('Bombeiros – Posto Santo Amaro','bombeiros', ST_MakePoint(-46.7089,-23.6537)::geography),
 ('Bombeiros – Posto Penha','bombeiros', ST_MakePoint(-46.551,-23.528)::geography),
 ('Bombeiros – Posto Itaquera','bombeiros', ST_MakePoint(-46.471,-23.547)::geography),
 ('Bombeiros – Posto Santana','bombeiros', ST_MakePoint(-46.626,-23.499)::geography),
 ('SAMU – Base Lapa','samu', ST_MakePoint(-46.702,-23.523)::geography),
 ('SAMU – Base Vila Mariana','samu', ST_MakePoint(-46.634,-23.589)::geography),
 ('SAMU – Base Guaianases','samu', ST_MakePoint(-46.41,-23.54)::geography),
 ('SAMU – Base Campo Limpo','samu', ST_MakePoint(-46.76,-23.648)::geography),
 ('SAMU – Base Freguesia','samu', ST_MakePoint(-46.69,-23.48)::geography),
 ('Defesa Civil – Regional Sé','defesa_civil', ST_MakePoint(-46.6333,-23.5505)::geography),
 ('Defesa Civil – Regional Butantã','defesa_civil', ST_MakePoint(-46.72,-23.57)::geography),
 ('Defesa Civil – Regional Itaquera','defesa_civil', ST_MakePoint(-46.43,-23.56)::geography),
 ('Defesa Civil – Regional M''Boi Mirim','defesa_civil', ST_MakePoint(-46.775,-23.69)::geography),
 ('CET – Central de Tráfego','cet', ST_MakePoint(-46.623,-23.574)::geography),
 ('CET – Base Radial Leste','cet', ST_MakePoint(-46.52,-23.543)::geography),
 ('CET – Base Marginal Pinheiros','cet', ST_MakePoint(-46.725,-23.56)::geography),
 ('Zeladoria – Subprefeitura Pinheiros','zeladoria', ST_MakePoint(-46.684,-23.564)::geography),
 ('Zeladoria – Subprefeitura Mooca','zeladoria', ST_MakePoint(-46.599,-23.556)::geography),
 ('Zeladoria – Subprefeitura Itaquera','zeladoria', ST_MakePoint(-46.46,-23.55)::geography),
 ('Zeladoria – Capela do Socorro','zeladoria', ST_MakePoint(-46.7,-23.72)::geography),
 ('Monitoramento ambiental – Cerqueira César','ambiental', ST_MakePoint(-46.668,-23.558)::geography),
 ('Monitoramento ambiental – Mooca','ambiental', ST_MakePoint(-46.595,-23.56)::geography);
