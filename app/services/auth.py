"""Login do painel com usuario e senha (igual Usuarios da CompactPay): admin
ve e mexe em tudo; cliente so ve os carregadores do proprio cliente.

Senha com PBKDF2-SHA256 (biblioteca padrao); token JWT HS256. A chave de
admin (X-Admin-Key) continua valendo como acesso de admin.
"""
import base64
import hashlib
import hmac
import os
from datetime import timedelta

import jwt
from fastapi import HTTPException

from app.core.config import settings
from app.models.carregador import agora

ITERACOES = 240_000
VALIDADE = timedelta(days=7)
PAPEIS = ("admin", "cliente")


def hash_senha(senha: str) -> str:
    sal = os.urandom(16)
    derivada = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal, ITERACOES)
    return f"pbkdf2${ITERACOES}${base64.b64encode(sal).decode()}${base64.b64encode(derivada).decode()}"


def conferir_senha(senha: str, guardado: str | None) -> bool:
    try:
        _, iteracoes, sal, derivada = (guardado or "").split("$")
        calculada = hashlib.pbkdf2_hmac("sha256", senha.encode(), base64.b64decode(sal), int(iteracoes))
        return hmac.compare_digest(calculada, base64.b64decode(derivada))
    except (ValueError, TypeError):
        return False


def _segredo() -> str:
    # Sem JWT_SECRET definido, deriva da ADMIN_API_KEY (trocar a chave derruba os logins).
    segredo = settings.JWT_SECRET or hashlib.sha256(f"jwt:{settings.ADMIN_API_KEY}".encode()).hexdigest()
    if not segredo or not (settings.JWT_SECRET or settings.ADMIN_API_KEY):
        raise HTTPException(status_code=500, detail="Configure JWT_SECRET ou ADMIN_API_KEY no servidor")
    return segredo


def criar_token(usuario) -> str:
    momento = agora()
    return jwt.encode(
        {"sub": str(usuario.id), "role": usuario.role, "cliente_id": usuario.cliente_id, "iat": momento, "exp": momento + VALIDADE},
        _segredo(),
        algorithm="HS256",
    )


def ler_token(token: str) -> dict:
    try:
        return jwt.decode(token, _segredo(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Sessao expirada. Entre de novo.") from exc
