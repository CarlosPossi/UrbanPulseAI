import json
import asyncpg
from .config import DATABASE_URL


async def _init(conn):
    for t in ("json", "jsonb"):
        await conn.set_type_codec(t, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(min_size=2, max_size=10) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=min_size, max_size=max_size, init=_init)
    # Migração aditiva e idempotente: preserva volumes/bancos já existentes.
    async with pool.acquire() as conn:
        await conn.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS smoke_ppm REAL")
        await conn.execute("ALTER TABLE readings ADD COLUMN IF NOT EXISTS flame_detected BOOLEAN")
        await conn.execute("UPDATE action_targets SET hazards = array_append(hazards, 'fire') WHERE kind = 'whatsapp' AND NOT ('fire' = ANY(hazards))")
        await conn.execute("""
            INSERT INTO sensors (id, name, api_key_hash, geom) VALUES
            ('F-001','Cantareira – Núcleo de mata', encode(sha256('dev-key-F-001'::bytea),'hex'), ST_MakePoint(-46.638,-23.395)::geography),
            ('F-002','Parque do Carmo – Área verde', encode(sha256('dev-key-F-002'::bytea),'hex'), ST_MakePoint(-46.472,-23.578)::geography),
            ('F-003','Horto Florestal – Zona norte', encode(sha256('dev-key-F-003'::bytea),'hex'), ST_MakePoint(-46.633,-23.466)::geography),
            ('F-004','APA Capivari-Monos – Zona sul', encode(sha256('dev-key-F-004'::bytea),'hex'), ST_MakePoint(-46.70,-23.86)::geography)
            ON CONFLICT (id) DO NOTHING
        """)
    return pool
