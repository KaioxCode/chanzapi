from __future__ import annotations

import random
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from .config import settings
from .db import api_key_exists, create_api_key

router = APIRouter(tags=["keys"])


@router.get("/gen/apikey/admin/json")
def generate_key(
    label: Optional[str] = Query(default=None, description="Rótulo opcional da key"),
    x_admin_secret: str | None = Header(default=None),
):
    if x_admin_secret != settings.admin_secret:
        raise HTTPException(status_code=401, detail="Admin secret inválido.")

    for _ in range(200):
        key = str(random.randint(1, 9999)).zfill(4)
        if not api_key_exists(key):
            item = create_api_key(api_key=key, label=label)
            return {
                "status": "success",
                "apikey": item["api_key"],
                "label": item["label"],
                "api_name": settings.api_name,
                "creator": settings.creator,
                "version": settings.api_version,
            }

    raise HTTPException(status_code=500, detail="Não foi possível gerar uma key única.")
