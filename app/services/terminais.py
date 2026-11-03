"""Chaves do tablet (header X-Terminal-Key) e senha OCPP dos carregadores.
Mesmo esquema do TerminalSmartApp da CompactPay: a chave aparece uma vez so,
no banco fica o sha256."""
import base64
import hashlib
import hmac
import secrets

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.carregador import Carregador, TerminalTablet, agora


def gerar_chave(prefixo: str = "evt_") -> str:
    return prefixo + secrets.token_urlsafe(32)


def hash_chave(chave: str) -> str:
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()


def autenticar_terminal(db: Session, chave: str | None) -> TerminalTablet:
    if not chave:
        raise HTTPException(status_code=401, detail="Terminal sem chave")
    terminal = (
        db.query(TerminalTablet)
        .filter(TerminalTablet.chave_hash == hash_chave(chave), TerminalTablet.ativo.is_(True))
        .first()
    )
    if not terminal:
        raise HTTPException(status_code=401, detail="Chave de terminal invalida")
    terminal.ultimo_uso_em = agora()
    db.commit()
    return terminal


def senha_ocpp_confere(carregador: Carregador, authorization: str | None) -> bool:
    """Basic Auth do OCPP 1.6 (Security Profile 1): usuario = ChargePointId.
    Carregador sem senha cadastrada aceita conexao sem Authorization."""
    if not carregador.ocpp_senha_hash:
        return True
    if not authorization or not authorization.lower().startswith("basic "):
        return False
    try:
        usuario, _, senha = base64.b64decode(authorization[6:].strip()).decode("utf-8").partition(":")
    except Exception:
        return False
    return usuario == carregador.id_hardware and hmac.compare_digest(hash_chave(senha), carregador.ocpp_senha_hash)
