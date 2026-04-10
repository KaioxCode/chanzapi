from __future__ import annotations

import secrets
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Query, Request

from .config import settings
from .db import get_api_key_record


_admin_sessions: dict[str, str] = {}


def validate_admin_credentials(username: str | None, password: str | None) -> bool:
    return username == settings.admin_username and password == settings.admin_password


def create_admin_session(username: str) -> str:
    token = secrets.token_hex(32)
    _admin_sessions[token] = username
    return token


def get_admin_user(admin_session: Optional[str] = Cookie(default=None)) -> str:
    if not admin_session or admin_session not in _admin_sessions:
        raise HTTPException(status_code=401, detail="Não autenticado.")
    return _admin_sessions[admin_session]


def require_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(default=None),
    apikey: Optional[str] = Query(default=None),
) -> str:
    api_key = x_api_key or apikey

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="API key obrigatória. Envie no header x-api-key ou na query ?apikey=",
        )

    key_record = get_api_key_record(api_key)
    if not key_record:
        raise HTTPException(status_code=401, detail="API key inválida.")

    if not key_record.get("active", False):
        raise HTTPException(status_code=403, detail="API key desativada.")

    return api_key