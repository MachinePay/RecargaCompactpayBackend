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


# ---- Motorista (app publico) ---------------------------------------------
# Assinado com outra chave: o token do motorista nunca abre o painel, e o do
# painel nunca vale no app.
VALIDADE_MOTORISTA = timedelta(days=90)


def _segredo_motorista() -> str:
    return hashlib.sha256(f"motorista:{_segredo()}".encode()).hexdigest()


def criar_token_motorista(motorista) -> str:
    momento = agora()
    return jwt.encode(
        {"sub": str(motorista.id), "tipo": "motorista", "iat": momento, "exp": momento + VALIDADE_MOTORISTA},
        _segredo_motorista(),
        algorithm="HS256",
    )


def ler_token_motorista(token: str) -> int | None:
    """Id do motorista, ou None se o token nao vale (vencido, de outro tipo)."""
    try:
        dados = jwt.decode(token, _segredo_motorista(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if dados.get("tipo") != "motorista":
        return None
    try:
        return int(dados.get("sub"))
    except (TypeError, ValueError):
        return None
