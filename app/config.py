from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_name: str = os.getenv("API_NAME", "ConsultaDataAPI")
    api_version: str = os.getenv("API_VERSION", "1.0.0")
    creator: str = os.getenv("CREATOR", "SeuNome")
    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "troque_essa_senha_forte")
    admin_secret: str = os.getenv("ADMIN_SECRET", "troque-este-segredo-interno")
    session_secret: str = os.getenv("SESSION_SECRET", "troque-esta-chave-de-sessao")
    base44_webhook_url: str = os.getenv("BASE44_WEBHOOK_URL", "")
    base44_webhook_secret: str = os.getenv("BASE44_WEBHOOK_SECRET", "")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/consulta_api.db")
    query_json_dir: str = os.getenv("QUERY_JSON_DIR", "./data/query_dbs")


settings = Settings()
