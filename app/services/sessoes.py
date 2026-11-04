"""Sessao de carga: liga o carregador depois do pagamento e desliga quando o
tempo pago acaba.

O tempo corre no SERVIDOR, a partir do StartTransaction (carro plugado e
carregando), nao do pagamento. Se o tablet travar ou cair, o worker daqui
continua e manda o RemoteStopTransaction na hora certa. Se o carregador
estiver offline nessa hora, o stop e' reenviado ate ele voltar e confirmar
com StopTransaction.
"""
import logging
import threading
import time
from datetime import timedelta

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cobranca, SessaoCarga, agora
from app.services import ocpp_server, pagamentos, trava
from app.services.ocpp_handlers import STATUS_SESSAO_ATIVA, registrar_historico

START_RETRY_SECONDS = 10
# Depois de tantas tentativas de stop sem StopTransaction, avisa no historico.
STOP_TENTATIVAS_ALERTA = 4
# Status do conector em que ele com certeza nao esta entregando energia.
CONECTOR_PARADO = ("Available", "Finishing", "Preparing", "Unavailable", "Faulted")


def mudar_status(db, sessao: SessaoCarga, de: tuple[str, ...], para: str, **campos) -> bool:
    """Troca de status com UPDATE condicional: o worker, os callbacks do OCPP e
    os handlers mexem na mesma sessao de threads diferentes, e um nao pode
    desfazer o que o outro acabou de gravar (ex.: estornar uma sessao que
    acabou de comecar a carregar)."""
    alterou = (
        db.query(SessaoCarga)
        .filter(SessaoCarga.id == sessao.id, SessaoCarga.status.in_(de))
        .update({"status": para, **campos}, synchronize_session=False)
    )
    db.commit()
    db.refresh(sessao)
    return alterou == 1


def sessao_ativa(db, carregador_id: str) -> SessaoCarga | None:
    return (
        db.query(SessaoCarga)
        .filter(SessaoCarga.carregador_id == carregador_id, SessaoCarga.status.in_(STATUS_SESSAO_ATIVA))
        .order_by(SessaoCarga.id.desc())
        .first()
    )


def iniciar_sessao_para_cobranca(db, cobranca: Cobranca) -> SessaoCarga:
    """Chamada uma vez por cobranca aprovada (pagamentos.aprovar_cobranca)."""
    carregador = db.get(Carregador, cobranca.carregador_id)
    ocupado = sessao_ativa(db, cobranca.carregador_id)
    sessao = SessaoCarga(
        carregador_id=cobranca.carregador_id,
        cobranca_id=cobranca.id,
        connector_id=carregador.connector_id,
        id_tag="pendente",
        minutos_pagos=cobranca.minutos,
        status="aguardando_inicio",
    )
    db.add(sessao)
    db.flush()
    # idTag tem no maximo 20 caracteres no OCPP 1.6.
    sessao.id_tag = f"EV{sessao.id}"
    db.commit()

    if ocupado is not None:
        # Pagamento aprovado tarde (ex.: cobranca expirada paga mesmo assim)
        # com outra carga em andamento: nao da pra liberar.
        falhar_sessao(db, sessao, "carregador_ocupado")
        return sessao
    enviar_remote_start(db, sessao)
    return sessao


def enviar_remote_start(db, sessao: SessaoCarga) -> bool:
    # O conector fica travado fora de recarga paga (services/trava.py): libera antes de ligar.
    trava.destravar_para_venda(sessao.carregador_id, sessao.connector_id)
    futuro = ocpp_server.enviar_chamada(
        sessao.carregador_id,
        "RemoteStartTransaction",
        {"connectorId": sessao.connector_id, "idTag": sessao.id_tag},
    )
    sessao.start_enviado_em = agora()
    db.commit()
    if futuro is None:
        return False
    sessao_id = sessao.id
    futuro.add_done_callback(lambda f: _ao_responder_remote_start(sessao_id, f))
    return True


def _ao_responder_remote_start(sessao_id: int, futuro) -> None:
    if futuro.exception() is not None:
        return  # sem resposta: o worker reenvia
    status = (futuro.result() or {}).get("status")
    db = SessionLocal()
    try:
        sessao = db.get(SessaoCarga, sessao_id)
        if sessao is None:
            return
        if status == "Accepted":
            # Se o StartTransaction ja chegou antes desta resposta, nao muda nada.
            if mudar_status(db, sessao, ("aguardando_inicio",), "aguardando_carro"):
                registrar_historico(db, sessao.carregador_id, "SESSAO", f"Sessao {sessao.id}: carregador aceitou o inicio")
                db.commit()
        else:
            falhar_sessao(db, sessao, "remote_start_rejeitado")
    finally:
        db.close()


def encerrar_sessao(db, sessao: SessaoCarga, motivo: str = "tempo_esgotado") -> bool:
    """Manda RemoteStopTransaction. A sessao so vira finalizada quando o
    carregador confirmar com StopTransaction."""
    if sessao.status in ("aguardando_inicio", "aguardando_carro"):
        # Ainda nao ha transacao no carregador para parar.
        falhar_sessao(db, sessao, motivo)
        return True
    if not mudar_status(
        db,
        sessao,
        ("carregando", "encerrando"),
        "encerrando",
        motivo_fim=sessao.motivo_fim or motivo,
        stop_tentativas=sessao.stop_tentativas + 1,
        ultimo_stop_em=agora(),
    ):
        return False
    if sessao.stop_tentativas == STOP_TENTATIVAS_ALERTA:
        registrar_historico(
            db,
            sessao.carregador_id,
            "SESSAO",
            f"ALERTA: sessao {sessao.id} ainda sem StopTransaction apos {sessao.stop_tentativas} tentativas de parar",
        )
    db.commit()
    futuro = ocpp_server.enviar_chamada(sessao.carregador_id, "RemoteStopTransaction", {"transactionId": sessao.id})
    if futuro is None:
        return False
    sessao_id = sessao.id
    futuro.add_done_callback(lambda f: _ao_responder_remote_stop(sessao_id, f))
    return True


def _ao_responder_remote_stop(sessao_id: int, futuro) -> None:
    if futuro.exception() is not None:
        return
    aceito = (futuro.result() or {}).get("status") == "Accepted"
    db = SessionLocal()
    try:
        sessao = db.get(SessaoCarga, sessao_id)
        if sessao is None or sessao.status != "encerrando":
            return
        if aceito:
            # Normal: esperar o StopTransaction. Mas o JST-901 responde Accepted
            # tambem para transacao que ja encerrou (e cujo StopTransaction se
            # perdeu), e ai nunca mais manda o Stop. Com varias tentativas
            # aceitas e o conector fora de carga, da a sessao por encerrada.
            carregador = db.get(Carregador, sessao.carregador_id)
            conector_parado = carregador is not None and carregador.status_conector in CONECTOR_PARADO
            if sessao.stop_tentativas >= 3 and conector_parado:
                if mudar_status(db, sessao, ("encerrando",), "finalizada", fim_real=agora(), motivo_fim="stop_sem_confirmacao"):
                    registrar_historico(
                        db,
                        sessao.carregador_id,
                        "SESSAO",
                        f"ATENCAO: sessao {sessao.id} encerrada sem StopTransaction (carregador aceitou o stop e esta {carregador.status_conector})",
                    )
                    db.commit()
            return
        registrar_historico(db, sessao.carregador_id, "SESSAO", f"Sessao {sessao.id}: carregador recusou o RemoteStop")
        db.commit()
        # Recusa repetida costuma ser transacao que o carregador ja encerrou
        # (e o StopTransaction se perdeu). Fecha aqui para nao insistir pra sempre.
        if sessao.stop_tentativas >= 3:
            mudar_status(db, sessao, ("encerrando",), "finalizada", fim_real=agora(), motivo_fim="stop_recusado_verificar")
    finally:
        db.close()


def falhar_sessao(db, sessao: SessaoCarga, motivo: str) -> None:
    """So falha sessao que ainda nao comecou a carregar; estorna o pagamento."""
    if not mudar_status(db, sessao, ("aguardando_inicio", "aguardando_carro"), "falhou", fim_real=agora(), motivo_fim=motivo):
        return
    registrar_historico(db, sessao.carregador_id, "SESSAO", f"Sessao {sessao.id} falhou: {motivo}")
    db.commit()
    if settings.AUTO_ESTORNO:
        cobranca = db.get(Cobranca, sessao.cobranca_id)
        pagamentos.estornar_cobranca(db, cobranca)


def segundos_restantes(sessao: SessaoCarga) -> int | None:
    if sessao.status != "carregando" or sessao.fim_previsto is None:
        return None
    return max(0, int((sessao.fim_previsto - agora()).total_seconds()))


# ---- Worker -------------------------------------------------------------

def processar_sessoes_uma_vez() -> None:
    db = SessionLocal()
    try:
        momento = agora()
        limite_inicio = momento - timedelta(seconds=settings.TEMPO_MAXIMO_INICIO_SEGUNDOS)

        for sessao in db.query(SessaoCarga).filter(SessaoCarga.status == "aguardando_inicio").all():
            if sessao.created_at <= limite_inicio:
                falhar_sessao(db, sessao, "carregador_nao_respondeu")
            elif (
                sessao.start_enviado_em is None
                or sessao.start_enviado_em <= momento - timedelta(seconds=START_RETRY_SECONDS)
            ) and ocpp_server.esta_conectado(sessao.carregador_id):
                enviar_remote_start(db, sessao)

        for sessao in db.query(SessaoCarga).filter(SessaoCarga.status == "aguardando_carro").all():
            if sessao.created_at <= limite_inicio and sessao.minutos_pagos > 0:
                falhar_sessao(db, sessao, "carro_nao_conectado")

        vencidas = (
            db.query(SessaoCarga)
            .filter(SessaoCarga.status == "carregando", SessaoCarga.fim_previsto.isnot(None), SessaoCarga.fim_previsto <= momento)
            .all()
        )
        for sessao in vencidas:
            encerrar_sessao(db, sessao, "tempo_esgotado")

        limite_stop = momento - timedelta(seconds=settings.STOP_RETRY_SEGUNDOS)
        for sessao in db.query(SessaoCarga).filter(SessaoCarga.status == "encerrando").all():
            if (sessao.ultimo_stop_em is None or sessao.ultimo_stop_em <= limite_stop) and ocpp_server.esta_conectado(
                sessao.carregador_id
            ):
                encerrar_sessao(db, sessao)

        # Sem recarga paga, conector travado (nao carrega ao plugar).
        trava.verificar(db)
    finally:
        db.close()


def run_sessoes_worker() -> None:
    logging.info("Worker de sessoes de carga iniciado")
    while True:
        try:
            processar_sessoes_uma_vez()
        except Exception:
            logging.exception("Erro no worker de sessoes de carga")
        time.sleep(settings.SESSOES_WORKER_INTERVAL_SECONDS)


def start_sessoes_worker() -> threading.Thread | None:
    if not settings.START_SESSOES_WORKER:
        logging.info("Worker de sessoes desativado por START_SESSOES_WORKER=false")
        return None
    thread = threading.Thread(target=run_sessoes_worker, daemon=True)
    thread.start()
    return thread
