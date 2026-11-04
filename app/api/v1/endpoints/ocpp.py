"""Endpoint WebSocket onde o carregador se conecta.

URL configurada no app do fabricante: ws://<servidor>/ocpp/<ChargePointId>
(em producao wss://). Alguns firmwares colam prefixos no caminho
(/ocpp/v16/<id>); o ChargePointId e' sempre o ultimo pedaco.
"""
import logging

from fastapi import APIRouter, WebSocket
from starlette.concurrency import run_in_threadpool

from app.db.session import SessionLocal
from app.models.carregador import Carregador
from app.services import diagnostico, ocpp_server, terminais

router = APIRouter()

SUBPROTOCOLO = "ocpp1.6"


def ip_cliente(conexao) -> str | None:
    """IP de quem chamou. Atras do proxy da Render vem no X-Forwarded-For."""
    encaminhado = conexao.headers.get("x-forwarded-for")
    if encaminhado:
        return encaminhado.split(",")[0].strip()
    return conexao.client.host if conexao.client else None


def _motivo_recusa(charge_point_id: str, authorization: str | None) -> str | None:
    """None = pode conectar. Senao, o motivo (vai para o diagnostico)."""
    db = SessionLocal()
    try:
        carregador = db.get(Carregador, charge_point_id)
        if carregador is None or carregador.excluido_em is not None:
            return "nao_cadastrado"
        if not carregador.ativo:
            return "inativo"
        if not terminais.senha_ocpp_confere(carregador, authorization):
            return "senha_invalida" if authorization else "sem_senha"
        return None
    finally:
        db.close()


MENSAGENS_RECUSA = {
    "sem_id": "Conexao sem ID no endereco",
    "nao_cadastrado": "ID nao cadastrado no painel",
    "inativo": "Carregador marcado como inativo no painel",
    "senha_invalida": "Senha OCPP errada",
    "sem_senha": "Carregador cadastrado com senha, mas conectou sem senha",
}


@router.websocket("/ocpp/{caminho:path}")
# O app do JST-901 so tem "endereco + porta + pile ID"; dependendo do firmware
# a URL sai sem o /ocpp (ws://host:porta/<id>). Aceita na raiz tambem.
@router.websocket("/{caminho:path}")
async def ocpp_websocket(websocket: WebSocket, caminho: str):
    ip = ip_cliente(websocket)
    charge_point_id = caminho.rstrip("/").split("/")[-1]
    pedidos = [p.strip() for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    logging.info("[OCPP] tentativa de conexao em '/%s' de %s", caminho, ip)
    diagnostico.registrar(
        "tentativa",
        charge_point_id or None,
        f"Tentativa de conexao em /{caminho} (subprotocolo: {', '.join(pedidos) or 'nenhum'})",
        ip=ip,
    )

    motivo = "sem_id" if not charge_point_id else await run_in_threadpool(
        _motivo_recusa, charge_point_id, websocket.headers.get("authorization")
    )
    if motivo:
        logging.warning("[OCPP] conexao recusada (%s): '%s'", motivo, charge_point_id)
        diagnostico.registrar(
            "recusada", charge_point_id or None, MENSAGENS_RECUSA[motivo], nivel="erro", acao=motivo, ip=ip
        )
        await websocket.close(code=1008)
        return

    # Carregador barato as vezes nao manda o subprotocolo; aceita mesmo assim.
    await websocket.accept(subprotocol=SUBPROTOCOLO if SUBPROTOCOLO in pedidos else None)
    logging.info("[OCPP] carregador '%s' conectado", charge_point_id)
    diagnostico.registrar("aceita", charge_point_id, "Conexao aceita: carregador online", nivel="ok", ip=ip)
    await ocpp_server.atender_conexao(websocket, charge_point_id, ip)
    logging.info("[OCPP] carregador '%s' desconectado", charge_point_id)
    diagnostico.registrar("desconectou", charge_point_id, "Carregador desconectou", nivel="aviso", ip=ip)
