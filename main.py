import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

DB_PATH = Path(os.getenv("TRAINER_DB_PATH", "data/trainer.db"))
DATABASE_URL = os.getenv("DATABASE_URL")
PARAM = "%s" if DATABASE_URL else "?"
BASE_URL = os.getenv("TRAINER_BASE_URL", "http://localhost:8000")

SOURCES = {
    "strava": {
        "client_id": "STRAVA_CLIENT_ID",
        "client_secret": "STRAVA_CLIENT_SECRET",
        "authorize": "https://www.strava.com/oauth/authorize",
        "token": "https://www.strava.com/oauth/token",
    },
    "oura": {
        "client_id": "OURA_CLIENT_ID",
        "client_secret": "OURA_CLIENT_SECRET",
        "authorize": "https://cloud.ouraring.com/oauth/authorize",
        "token": "https://api.ouraring.com/oauth/token",
    },
}


def db():
    if DATABASE_URL:
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def bootstrap_env_connections() -> None:
    for source in SOURCES:
        prefix = source.upper()
        access_token = os.getenv(f"{prefix}_ACCESS_TOKEN")
        if access_token:
            save_connection(source, {
                "access_token": access_token,
                "refresh_token": os.getenv(f"{prefix}_REFRESH_TOKEN"),
                "expires_at": os.getenv(f"{prefix}_EXPIRES_AT"),
                "scope": os.getenv(f"{prefix}_SCOPE"),
            })


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    statements = [
        """CREATE TABLE IF NOT EXISTS source_records (
            source TEXT NOT NULL, kind TEXT NOT NULL, external_id TEXT NOT NULL,
            recorded_at TEXT, payload TEXT NOT NULL,
            fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (source, kind, external_id)
        )""",
        """CREATE TABLE IF NOT EXISTS sync_state (
            source TEXT PRIMARY KEY, cursor TEXT, synced_at TEXT, error TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS connections (
            source TEXT PRIMARY KEY, access_token TEXT NOT NULL, refresh_token TEXT,
            expires_at INTEGER, scope TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS plan_state (
            state_key TEXT PRIMARY KEY, payload TEXT NOT NULL
        )""", 
    ]
    with db() as connection:
        for statement in statements:
            connection.execute(statement)
    bootstrap_env_connections()


def require_source(source: str) -> dict:
    try:
        return SOURCES[source]
    except KeyError:
        raise HTTPException(404, f"Fuente no soportada: {source}")


def connection_for(source: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(
            f"SELECT * FROM connections WHERE source = {PARAM}", (source,)
        ).fetchone()


def save_connection(source: str, tokens: dict) -> None:
    current = connection_for(source)
    expires_at = tokens.get("expires_at")
    if not expires_at and tokens.get("expires_in"):
        expires_at = int(time.time()) + int(tokens["expires_in"])
    with db() as connection:
        connection.execute(
            f"""INSERT INTO connections(source, access_token, refresh_token, expires_at, scope)
               VALUES ({PARAM}, {PARAM}, {PARAM}, {PARAM}, {PARAM})
               ON CONFLICT(source) DO UPDATE SET access_token=excluded.access_token,
                 refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                 scope=excluded.scope""",
            (source, tokens["access_token"], tokens.get("refresh_token")
             or (current["refresh_token"] if current else None), expires_at,
             tokens.get("scope") or (current["scope"] if current else None)),
        )


def save_records(source: str, kind: str, records: list[dict]) -> int:
    inserted = 0
    with db() as connection:
        for record in records:
            external_id = str(record.get("id") or record.get("activityId") or hashlib.sha256(
                json.dumps(record, sort_keys=True).encode()
            ).hexdigest()[:24])
            cursor = connection.execute(
                f"""INSERT INTO source_records
                   (source, kind, external_id, recorded_at, payload)
                   VALUES ({PARAM}, {PARAM}, {PARAM}, {PARAM}, {PARAM})
                   ON CONFLICT (source, kind, external_id) DO NOTHING""",
                (source, kind, external_id,
                 record.get("start_date") or record.get("start_datetime")
                 or record.get("day") or record.get("start_date_local"),
                 json.dumps(record, ensure_ascii=False)),
            )
            inserted += cursor.rowcount
    return inserted


async def refresh_connection(source: str) -> None:
    config = require_source(source)
    connection = connection_for(source)
    client_id = os.getenv(config["client_id"])
    client_secret = os.getenv(config["client_secret"])
    if not connection or not connection["refresh_token"] or not client_id or not client_secret:
        raise HTTPException(401, f"No se puede renovar {source}; vuelve a conectar")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(config["token"], data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": connection["refresh_token"],
        })
        response.raise_for_status()
        save_connection(source, response.json())


async def fetch_json(source: str, url: str, **params) -> dict | list:
    connection = connection_for(source)
    if not connection:
        raise HTTPException(400, f"Conecta primero {source}: /connect/{source}")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            url, params=params, headers={"Authorization": f"Bearer {connection['access_token']}"}
        )
        if response.status_code == 401:
            await refresh_connection(source)
            connection = connection_for(source)
            response = await client.get(
                url, params=params,
                headers={"Authorization": f"Bearer {connection['access_token']}"},
            )
        response.raise_for_status()
        return response.json()


async def sync_strava() -> int:
    connection = connection_for("strava")
    scopes = set((connection["scope"] or "").replace(",", " ").split()) if connection else set()
    if not scopes.intersection({"activity:read", "activity:read_all"}):
        return 0
    total = 0
    page = 1
    while True:
        records = await fetch_json(
            "strava", "https://www.strava.com/api/v3/athlete/activities",
            page=page, per_page=200,
        )
        if not records:
            break
        total += save_records("strava", "activity", records)
        page += 1
    return total


async def sync_oura() -> int:
    total = 0
    start = date.today() - timedelta(days=240)
    end = date.today()
    for kind in ("daily_activity", "daily_readiness", "daily_sleep", "sleep", "workout", "heartrate"):
        token = None
        while True:
            params = {"start_date": start.isoformat(), "end_date": end.isoformat()}
            if token:
                params["next_token"] = token
            response = await fetch_json(
                "oura", f"https://api.ouraring.com/v2/usercollection/{kind}", **params
            )
            records = response.get("data", [])
            total += save_records("oura", kind, records)
            token = response.get("next_token")
            if not token:
                break
    return total


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Trainer", lifespan=lifespan)


@app.get("/")
def index() -> dict[str, object]:
    return {
        "status": "ok",
        "connect": {source: f"/connect/{source}" for source in SOURCES},
        "sync": "/sync",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/connect/{source}")
def connect(source: str):
    config = require_source(source)
    client_id = os.getenv(config["client_id"])
    if not client_id:
        raise HTTPException(500, f"Falta la variable {config['client_id']}")
    scopes = "activity:read_all" if source == "strava" else "daily heartrate personal workout"
    params = {
        "client_id": client_id,
        "redirect_uri": f"{BASE_URL}/oauth/{source}/callback",
        "response_type": "code",
        "scope": scopes,
        "approval_prompt": "force" if source == "strava" else "auto",
        "state": secrets.token_urlsafe(16),
    }
    return RedirectResponse(f"{config['authorize']}?{urlencode(params)}")


@app.get("/oauth/{source}/callback")
async def oauth_callback(source: str, code: str, error: str | None = None):
    config = require_source(source)
    if error:
        raise HTTPException(400, error)
    client_id = os.getenv(config["client_id"])
    client_secret = os.getenv(config["client_secret"])
    if not client_id or not client_secret:
        raise HTTPException(500, "Faltan credenciales OAuth en el entorno")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(config["token"], data={
            "client_id": client_id, "client_secret": client_secret,
            "code": code, "grant_type": "authorization_code",
            "redirect_uri": f"{BASE_URL}/oauth/{source}/callback",
        })
        response.raise_for_status()
        save_connection(source, response.json())
    return {"connected": source, "next": f"/sync/{source}"}


@app.post("/sync/{source}")
async def sync(source: str):
    require_source(source)
    count = await (sync_strava() if source == "strava" else sync_oura())
    return {"source": source, "new_records": count}


@app.post("/sync")
async def sync_all():
    return {"strava": await sync_strava(), "oura": await sync_oura()}
