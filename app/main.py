from __future__ import annotations

import asyncio
import json
import os
import queue
import re
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from .auth import create_admin_session, get_admin_user, require_api_key, validate_admin_credentials
from .config import settings
from .db import (
    deactivate_api_key,
    get_dashboard_stats,
    init_db,
    list_api_keys,
    list_query_logs,
    register_listener,
    save_query_log,
    unregister_listener,
)
from .genkey import router as key_router

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title=settings.api_name,
    version=settings.api_version,
    description="""
API privada de consultas com autenticação por API Key, logs em tempo real,
painel administrativo, exportação de logs e documentação Swagger.

## Recursos
- Geração de API Keys
- Painel admin privado
- Logs em tempo real via SSE
- Consulta de CEP
- Consulta de IP
- Consulta de Nome
- Consulta de CNPJ
- Consulta de Placa
- Consulta de Telefone
- Consulta de Email
- Consulta de CPF (Datapro)
- Consulta de CPF (DirectD)
""",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={
        "name": settings.creator,
    },
    license_info={
        "name": "Private Use",
    },
)

app.include_router(key_router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "953247521fmshb9c525d84b6f59fp1c4b89jsnf768371fd7fd")
DIRECTD_TOKEN = os.getenv("DIRECTD_TOKEN", "DCFE6E75-73DF-466A-91D8-0FB732BD8636")
INVERTEXTO_TOKEN = os.getenv("INVERTEXTO_TOKEN", "25669|SEr08BkYg6P6LKb88QLAVfNMDQdFGHxI")


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse, tags=["Público"], summary="Página inicial")
def home() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/admin", response_class=HTMLResponse, tags=["Admin"], summary="Página do painel admin")
def admin_page() -> str:
    return (BASE_DIR / "static" / "admin.html").read_text(encoding="utf-8")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/status", tags=["Público"], summary="Status da API")
def status() -> dict:
    return {
        "status": "online",
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }


@app.post("/admin/login", tags=["Admin"], summary="Login admin")
async def admin_login(request: Request) -> JSONResponse:
    body = await request.json()
    username = body.get("username")
    password = body.get("password")

    if not validate_admin_credentials(username, password):
        raise HTTPException(status_code=401, detail="Usuário ou senha inválidos.")

    token = create_admin_session(username)
    response = JSONResponse({"status": "success", "message": "Login realizado com sucesso."})
    response.set_cookie(
        key="admin_session",
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=60 * 60 * 12,
    )
    return response


@app.post("/admin/logout", tags=["Admin"], summary="Logout admin")
def admin_logout(_: str = Depends(get_admin_user)) -> JSONResponse:
    response = JSONResponse({"status": "success", "message": "Logout realizado."})
    response.delete_cookie("admin_session")
    return response


@app.get("/admin/me", tags=["Admin"], summary="Usuário admin autenticado")
def admin_me(username: str = Depends(get_admin_user)) -> dict:
    return {"status": "success", "username": username}


@app.get("/admin/stats", tags=["Admin"], summary="Estatísticas do dashboard")
def admin_stats(_: str = Depends(get_admin_user)) -> dict:
    return {
        "status": "success",
        "api_name": settings.api_name,
        "creator": settings.creator,
        "stats": get_dashboard_stats(),
    }


@app.get("/admin/keys", tags=["Admin"], summary="Listar API keys")
def admin_keys(_: str = Depends(get_admin_user)) -> dict:
    return {"status": "success", "items": list_api_keys()}


@app.post("/admin/keys/{api_key}/deactivate", tags=["Admin"], summary="Desativar API key")
def admin_deactivate_key(api_key: str, _: str = Depends(get_admin_user)) -> dict:
    ok = deactivate_api_key(api_key)
    if not ok:
        raise HTTPException(status_code=404, detail="Key não encontrada.")
    return {"status": "success", "message": f"Key {api_key} desativada."}


@app.get("/admin/logs", tags=["Admin"], summary="Listar logs")
def admin_logs(limit: int = 100, _: str = Depends(get_admin_user)) -> dict:
    return {"status": "success", "items": list_query_logs(limit=limit)}


@app.get("/admin/stream", tags=["Admin"], summary="Stream em tempo real")
async def admin_stream(_: str = Depends(get_admin_user)) -> EventSourceResponse:
    listener = register_listener()

    async def event_generator():
        try:
            while True:
                try:
                    item = listener.get(timeout=15)
                    yield {"event": item["event"], "data": json.dumps(item["log"], ensure_ascii=False)}
                except queue.Empty:
                    yield {"event": "ping", "data": "{}"}
                await asyncio.sleep(0.05)
        finally:
            unregister_listener(listener)

    return EventSourceResponse(event_generator())


@app.get("/admin/export/logs", tags=["Admin"], summary="Exportar logs")
def export_logs(_: str = Depends(get_admin_user)) -> FileResponse:
    logs_path = BASE_DIR.parent / "data" / "export_logs.json"
    logs_path.parent.mkdir(parents=True, exist_ok=True)
    logs_path.write_text(
        json.dumps(list_query_logs(limit=1000), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return FileResponse(logs_path, filename="logs.json", media_type="application/json")


@app.get(
    "/cep/{cep}/json",
    tags=["Consultas"],
    summary="Consultar CEP",
    description="Consulta CEP via ViaCEP.",
)
def consulta_cep(cep: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    cep_digits = re.sub(r"\D", "", cep)
    if not re.fullmatch(r"\d{8}", cep_digits):
        raise HTTPException(status_code=400, detail="CEP inválido. Envie exatamente 8 dígitos.")

    try:
        via_cep_response = requests.get(f"https://viacep.com.br/ws/{cep_digits}/json/", timeout=10)
        via_cep_response.raise_for_status()
        data = via_cep_response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de CEP.") from exc

    if data.get("erro") is True:
        raise HTTPException(status_code=404, detail="CEP não encontrado.")

    result = {
        "status": "success",
        "query_type": "cep",
        "query": cep_digits,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="cep",
        query_value=cep_digits,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )
    return result


@app.get(
    "/ip/{ip}/json",
    tags=["Consultas"],
    summary="Consultar IP",
    description="Consulta IP público com geolocalização e provedor.",
)
def consulta_ip(ip: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    ip_clean = ip.strip()

    try:
        ip_response = requests.get(f"http://ip-api.com/json/{ip_clean}", timeout=10)
        ip_response.raise_for_status()
        data = ip_response.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de IP.") from exc

    if data.get("status") != "success":
        raise HTTPException(status_code=404, detail="IP não encontrado.")

    result = {
        "status": "success",
        "query_type": "ip",
        "query": ip_clean,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="ip",
        query_value=ip_clean,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result


@app.get(
    "/nome/{nome}/json",
    tags=["Consultas"],
    summary="Consultar nome",
    description="Consulta nome usando RapidAPI.",
)
def consulta_nome(nome: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    nome_clean = nome.strip()

    if not RAPIDAPI_KEY:
        raise HTTPException(status_code=500, detail="RAPIDAPI_KEY não configurada no .env")

    try:
        url = f"https://consulta-cpf-e-nome.p.rapidapi.com/BuscaNome/{nome_clean}"
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": "consulta-cpf-e-nome.p.rapidapi.com",
            "Content-Type": "application/json",
        }

        nome_response = requests.get(url, headers=headers, timeout=15)
        nome_response.raise_for_status()
        data = nome_response.json()

    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de nome.") from exc

    result = {
        "status": "success",
        "query_type": "nome",
        "query": nome_clean,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="nome",
        query_value=nome_clean,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result


@app.get(
    "/cnpj/{cnpj}/json",
    tags=["Consultas"],
    summary="Consultar CNPJ",
    description="Consulta CNPJ usando BrasilAPI.",
)
def consulta_cnpj(cnpj: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    cnpj_digits = re.sub(r"\D", "", cnpj)
    if not re.fullmatch(r"\d{14}", cnpj_digits):
        raise HTTPException(status_code=400, detail="CNPJ inválido. Envie exatamente 14 dígitos.")

    try:
        brasilapi_response = requests.get(
            f"https://brasilapi.com.br/api/cnpj/v1/{cnpj_digits}",
            timeout=10
        )
        brasilapi_response.raise_for_status()
        data = brasilapi_response.json()
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise HTTPException(status_code=404, detail="CNPJ não encontrado.") from exc
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de CNPJ.") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de CNPJ.") from exc

    result = {
        "status": "success",
        "query_type": "cnpj",
        "query": cnpj_digits,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="cnpj",
        query_value=cnpj_digits,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )
    return result


@app.get(
    "/placa/{placa}/json",
    tags=["Consultas"],
    summary="Consultar placa",
    description="Consulta veicular por placa usando DirectD.",
)
def consulta_placa(placa: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    placa_clean = placa.upper().strip()

    if not re.fullmatch(r"[A-Z]{3}[0-9][A-Z0-9][0-9]{2}|[A-Z]{3}[0-9]{4}", placa_clean):
        raise HTTPException(status_code=400, detail="Placa inválida.")

    if not DIRECTD_TOKEN:
        raise HTTPException(status_code=500, detail="DIRECTD_TOKEN não configurado no .env")

    try:
        placa_response = requests.get(
            f"https://apiv3.directd.com.br/api/ConsultaVeicular?PLACA={placa_clean}&TOKEN={DIRECTD_TOKEN}",
            timeout=15
        )

        placa_response.raise_for_status()
        data = placa_response.json()

    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de placa.") from exc

    result = {
        "status": "success",
        "query_type": "placa",
        "query": placa_clean,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="placa",
        query_value=placa_clean,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result


@app.get(
    "/telefone/{telefone}/json",
    tags=["Consultas"],
    summary="Consultar telefone",
    description="Consulta telefone usando RapidAPI.",
)
def consulta_telefone(telefone: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    telefone_digits = re.sub(r"\D", "", telefone)

    if not re.fullmatch(r"\d{10,11}", telefone_digits):
        raise HTTPException(status_code=400, detail="Telefone inválido. Envie 10 ou 11 dígitos.")

    if not RAPIDAPI_KEY:
        raise HTTPException(status_code=500, detail="RAPIDAPI_KEY não configurada no .env")

    try:
        url = f"https://consulta-cpf-e-nome.p.rapidapi.com/BuscaCPFTelefone/{telefone_digits}"
        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": "consulta-cpf-e-nome.p.rapidapi.com",
            "Content-Type": "application/json",
        }

        telefone_response = requests.get(url, headers=headers, timeout=15)
        telefone_response.raise_for_status()
        data = telefone_response.json()

    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de telefone.") from exc

    result = {
        "status": "success",
        "query_type": "telefone",
        "query": telefone_digits,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="telefone",
        query_value=telefone_digits,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result


@app.get(
    "/cpfdatapro/{cpf}/json",
    tags=["Consultas"],
    summary="Consultar CPF Datapro",
    description="Consulta CPF usando o endpoint Datapro via RapidAPI.",
)
def consulta_cpf_datapro(cpf: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    cpf_digits = re.sub(r"\D", "", cpf)

    if not re.fullmatch(r"\d{11}", cpf_digits):
        raise HTTPException(status_code=400, detail="CPF inválido. Envie exatamente 11 dígitos.")

    if not RAPIDAPI_KEY:
        raise HTTPException(status_code=500, detail="RAPIDAPI_KEY não configurada no .env")

    try:
        url = "https://cpf-datapro1.p.rapidapi.com/consultacpf"
        querystring = {
            "cpf": cpf_digits,
            "mode": "fast",
        }

        headers = {
            "x-rapidapi-key": RAPIDAPI_KEY,
            "x-rapidapi-host": "cpf-datapro1.p.rapidapi.com",
            "Content-Type": "application/json",
        }

        cpf_response = requests.get(url, headers=headers, params=querystring, timeout=15)
        cpf_response.raise_for_status()
        data = cpf_response.json()

    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de CPF Datapro.") from exc

    result = {
        "status": "success",
        "query_type": "cpf_datapro",
        "query": cpf_digits,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="cpf_datapro",
        query_value=cpf_digits,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result


@app.get(
    "/email/{email}/json",
    tags=["Consultas"],
    summary="Consultar email",
    description="Valida email usando Invertexto.",
)
def consulta_email(email: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    email_clean = email.strip()

    if not INVERTEXTO_TOKEN:
        raise HTTPException(status_code=500, detail="INVERTEXTO_TOKEN não configurado no .env")

    try:
        email_response = requests.get(
            f"https://api.invertexto.com/v1/email-validator/{email_clean}?token={INVERTEXTO_TOKEN}",
            timeout=10
        )
        email_response.raise_for_status()
        data = email_response.json()

    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de email.") from exc

    result = {
        "status": "success",
        "query_type": "email",
        "query": email_clean,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="email",
        query_value=email_clean,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result


@app.get(
    "/cpf/{cpf}/json",
    tags=["Consultas"],
    summary="Consultar CPF DirectD",
    description="Consulta CPF usando DirectD CadastroPessoaFisicaPlus.",
)
def consulta_cpf(cpf: str, request: Request, api_key: str = Depends(require_api_key)) -> dict:
    cpf_digits = re.sub(r"\D", "", cpf)

    if not re.fullmatch(r"\d{11}", cpf_digits):
        raise HTTPException(status_code=400, detail="CPF inválido. Envie exatamente 11 dígitos.")

    if not DIRECTD_TOKEN:
        raise HTTPException(status_code=500, detail="DIRECTD_TOKEN não configurado no .env")

    try:
        cpf_response = requests.get(
            f"https://apiv3.directd.com.br/api/CadastroPessoaFisicaPlus?CPF={cpf_digits}&TOKEN={DIRECTD_TOKEN}",
            timeout=15
        )

        cpf_response.raise_for_status()
        data = cpf_response.json()

    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Falha ao consultar serviço externo de CPF.") from exc

    result = {
        "status": "success",
        "query_type": "cpf",
        "query": cpf_digits,
        "data": data,
        "api_name": settings.api_name,
        "creator": settings.creator,
        "version": settings.api_version,
    }

    save_query_log(
        query_type="cpf",
        query_value=cpf_digits,
        response_data=result,
        status_code=200,
        api_key=api_key,
        ip_address=request.client.host if request.client else None,
    )

    return result