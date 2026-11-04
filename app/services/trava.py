"""Conector travado por padrao: so libera com recarga paga.

O JST-901 (firmware V1.4) carrega sozinho quando o carro e' plugado ("plug
and charge"), mesmo em Standard Mode. Para nao carregar de graca:

1. Fora de uma recarga paga, o servidor deixa o conector Inoperative
   (ChangeAvailability). Pelo OCPP 1.6 isso fica gravado no carregador e vale
   ate alguem mandar Operative, inclusive se ele reiniciar.
2. Na venda paga: Operative e, so depois da resposta, RemoteStartTransaction.
3. Quando a recarga termina (ou falha), o worker trava de novo.
4. Logo que o carregador conecta, tenta tambem as configuracoes padrao do
   OCPP que proibem carga sem autorizacao (quem nao suporta, ignora).
5. Reserva: se mesmo assim ele carregar sem recarga paga, ocpp_handlers manda
   parar (_bloquear_carga_sem_pagamento).

Estado em memoria (uma instancia so, igual as conexoes OCPP); some ao
reconectar e o worker manda tudo de novo.
"""
import logging
import threading
from datetime import timedelta

from app.core.config import settings
from app.models.carregador import agora

INTERVALO_TENTATIVA = timedelta(seconds=30)
ESPERA_DESTRAVAR_SEGUNDOS = 6
RESPOSTAS_OK = ("Accepted", "Scheduled")
# Configuracoes padrao do OCPP 1.6 contra carga sem autorizacao do servidor.
CONFIGURACOES = {
    "AuthorizeRemoteTxRequests": "true",
    "LocalPreAuthorize": "false",
    "LocalAuthorizeOffline": "false",
    "AllowOfflineTxForUnknownId": "false",
    "StopTransactionOnInvalidId": "true",
}

_lock = threading.Lock()
_estado: dict[str, dict] = {}


def _item(carregador_id: str) -> dict:
    return _estado.setdefault(carregador_id, {"boot": False, "travado": False, "tentativa": None, "config": False, "avisou_falha": False})


def conectou(carregador_id: str) -> None:
    with _lock:
        _estado.pop(carregador_id, None)


def apresentou(carregador_id: str) -> None:
    """BootNotification aceito: o carregador ja obedece comandos."""
    with _lock:
        _item(carregador_id)["boot"] = True


def marcar(carregador_id: str, esta_travado: bool) -> None:
    """Estado vindo de um comando manual (tela de testes)."""
    with _lock:
        item = _item(carregador_id)
        item["travado"] = esta_travado
        item["tentativa"] = agora()


def travado(carregador_id: str) -> bool:
    with _lock:
        return bool(_estado.get(carregador_id, {}).get("travado"))


def _historico(carregador_id: str, texto: str) -> None:
    from app.db.session import SessionLocal
    from app.services.ocpp_handlers import registrar_historico

    db = SessionLocal()
    try:
        registrar_historico(db, carregador_id, "DISPOSITIVO", texto)
        db.commit()
    finally:
        db.close()


def travar(carregador_id: str, conector: int, motivo: str = "sem recarga paga") -> None:
    """Manda Inoperative sem esperar; a resposta marca o estado."""
    from app.services import ocpp_server

    with _lock:
        item = _item(carregador_id)
        item["tentativa"] = agora()
    futuro = ocpp_server.enviar_chamada(carregador_id, "ChangeAvailability", {"connectorId": conector, "type": "Inoperative"})
    if futuro is None:
        return

    def resposta(f):
        status = None if f.exception() else (f.result() or {}).get("status")
        with _lock:
            item = _item(carregador_id)
            item["travado"] = status in RESPOSTAS_OK
            avisar = status not in RESPOSTAS_OK and not item["avisou_falha"]
            if avisar:
                item["avisou_falha"] = True
        if status in RESPOSTAS_OK:
            logging.info("[trava] %s travado (%s)", carregador_id, motivo)
        elif avisar:
            _historico(
                carregador_id,
                f"ALERTA: o carregador nao aceitou travar o conector ({status or 'sem resposta'}). "
                "Ele pode carregar ao plugar sem pagamento.",
            )

    futuro.add_done_callback(resposta)


def destravar_para_venda(carregador_id: str, conector: int) -> None:
    """Antes do RemoteStart de uma recarga paga: Operative e espera a resposta."""
    from app.services import ocpp_server

    if not travado(carregador_id):
        return
    futuro = ocpp_server.enviar_chamada(carregador_id, "ChangeAvailability", {"connectorId": conector, "type": "Operative"})
    if futuro is not None:
        try:
            futuro.result(timeout=ESPERA_DESTRAVAR_SEGUNDOS)
        except Exception:
            logging.warning("[trava] %s nao confirmou o Operative; manda o RemoteStart assim mesmo", carregador_id)
    with _lock:
        item = _item(carregador_id)
        item["travado"] = False
        item["tentativa"] = agora()  # nao trava de novo antes do RemoteStart sair


def _configurar(carregador_id: str) -> None:
    from app.services import ocpp_server

    for chave, valor in CONFIGURACOES.items():
        futuro = ocpp_server.enviar_chamada(carregador_id, "ChangeConfiguration", {"key": chave, "value": valor})
        if futuro is None:
            return

        def resposta(f, chave=chave):
            status = "sem resposta" if f.exception() else (f.result() or {}).get("status")
            logging.info("[trava] %s ChangeConfiguration %s: %s", carregador_id, chave, status)

        futuro.add_done_callback(resposta)


def verificar(db) -> None:
    """Chamado pelo worker de sessoes: trava quem esta conectado sem recarga paga."""
    if not settings.BLOQUEAR_CARGA_SEM_PAGAMENTO:
        return
    from app.models.carregador import Carregador, SessaoCarga
    from app.services import ocpp_server
    from app.services.ocpp_handlers import STATUS_SESSAO_ATIVA

    momento = agora()
    for carregador_id in ocpp_server.conectados():
        with _lock:
            item = _item(carregador_id)
            if not item["boot"] or item["travado"]:
                continue
            if item["tentativa"] and momento - item["tentativa"] < INTERVALO_TENTATIVA:
                continue
            configurar = not item["config"]
            item["config"] = True
        carregador = db.get(Carregador, carregador_id)
        if carregador is None:
            continue
        ativa = (
            db.query(SessaoCarga.id)
            .filter(SessaoCarga.carregador_id == carregador_id, SessaoCarga.status.in_(STATUS_SESSAO_ATIVA))
            .first()
        )
        if configurar:
            _configurar(carregador_id)
        if ativa is None:
            travar(carregador_id, carregador.connector_id)
