"""O que o servidor responde para cada mensagem que o CARREGADOR manda.

Roda em threadpool (ver ocpp_server.py); cada chamada abre a propria sessao
de banco. Os nomes dos campos seguem a especificacao OCPP 1.6J.
"""
import threading
from datetime import datetime, timedelta

from app.core.config import settings, tags_mestre
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cobranca, HistoricoOperacao, SessaoCarga, agora

STATUS_SESSAO_ANTES_DE_CARREGAR = ("aguardando_inicio", "aguardando_carro")
STATUS_SESSAO_ATIVA = ("aguardando_inicio", "aguardando_carro", "carregando", "encerrando")

# StopTransaction.reason (OCPP) -> motivo_fim gravado na sessao.
MOTIVOS_PARADA = {
    "Remote": "tempo_esgotado",
    "EVDisconnected": "carro_desconectado",
    "Local": "parado_no_carregador",
    "EmergencyStop": "parada_emergencia",
    "PowerLoss": "queda_de_energia",
    "Reboot": "carregador_reiniciou",
    "HardReset": "carregador_reiniciou",
    "SoftReset": "carregador_reiniciou",
    "DeAuthorized": "tag_recusada",
}


class AcaoNaoSuportada(Exception):
    pass


def _inteiro(valor) -> int | None:
    """Numero do OCPP. O firmware do JST-901 manda transactionId e meterStop
    como texto ("1", "0") em vez de numero; aceita os dois."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    try:
        return int(float(str(valor).strip()))
    except (TypeError, ValueError):
        return None


def _iso(momento: datetime) -> str:
    return momento.strftime("%Y-%m-%dT%H:%M:%S.") + f"{momento.microsecond // 1000:03d}Z"


def registrar_historico(db, carregador_id: str, categoria: str, descricao: str, valor: float | None = None) -> None:
    db.add(HistoricoOperacao(carregador_id=carregador_id, categoria=categoria, descricao=descricao, valor=valor))


def ao_conectar(charge_point_id: str) -> None:
    db = SessionLocal()
    try:
        carregador = db.get(Carregador, charge_point_id)
        if carregador:
            carregador.ultimo_sinal = agora()
            registrar_historico(db, charge_point_id, "DISPOSITIVO", "Carregador conectou no servidor OCPP")
            db.commit()
    finally:
        db.close()


def ao_desconectar(charge_point_id: str) -> None:
    db = SessionLocal()
    try:
        registrar_historico(db, charge_point_id, "DISPOSITIVO", "Carregador desconectou do servidor OCPP")
        db.commit()
    finally:
        db.close()


def tratar_chamada(charge_point_id: str, acao: str, payload: dict) -> dict:
    tratador = _TRATADORES.get(acao)
    if tratador is None:
        raise AcaoNaoSuportada(acao)
    db = SessionLocal()
    try:
        carregador = db.get(Carregador, charge_point_id)
        if carregador is None:
            # O endpoint ja recusa carregador nao cadastrado; defesa extra.
            raise RuntimeError(f"carregador {charge_point_id} nao cadastrado")
        carregador.ultimo_sinal = agora()
        resposta = tratador(db, carregador, payload)
        db.commit()
        return resposta
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---- Mensagens do carregador -------------------------------------------

def _boot_notification(db, carregador: Carregador, payload: dict) -> dict:
    carregador.vendor = payload.get("chargePointVendor") or carregador.vendor
    carregador.modelo = payload.get("chargePointModel") or carregador.modelo
    carregador.numero_serie = payload.get("chargePointSerialNumber") or payload.get("chargeBoxSerialNumber") or carregador.numero_serie
    carregador.firmware_version = payload.get("firmwareVersion") or carregador.firmware_version
    registrar_historico(
        db,
        carregador.id_hardware,
        "DISPOSITIVO",
        f"BootNotification: {carregador.vendor} {carregador.modelo} fw={carregador.firmware_version}",
    )
    return {"status": "Accepted", "currentTime": _iso(agora()), "interval": settings.OCPP_HEARTBEAT_INTERVAL}


def _heartbeat(db, carregador: Carregador, payload: dict) -> dict:
    return {"currentTime": _iso(agora())}


# Ultima tentativa de parar carga sem pagamento, por carregador (nao insiste a cada mensagem).
_ultimo_bloqueio: dict[str, datetime] = {}
INTERVALO_BLOQUEIO = timedelta(seconds=30)
STATUS_ENTREGANDO = ("Charging", "SuspendedEV", "SuspendedEVSE")


def liberar_bloqueio(carregador_id: str) -> bool:
    """True se o carregador tinha sido bloqueado (precisa voltar a Operative)."""
    return _ultimo_bloqueio.pop(carregador_id, None) is not None


def _tem_recarga_aberta(db, carregador: Carregador) -> bool:
    return (
        db.query(SessaoCarga.id)
        .filter(SessaoCarga.carregador_id == carregador.id_hardware, SessaoCarga.status.in_(STATUS_SESSAO_ATIVA))
        .first()
        is not None
    )


def _bloquear_carga_sem_pagamento(db, carregador: Carregador, transaction_id: int | None, motivo: str) -> None:
    """O JST-901 em "plug and charge" carrega ao plugar, sem StartTransaction
    (transactionId 0). Sem recarga paga aberta, manda parar: RemoteStop com o
    transactionId que ele usou e, de reserva, deixa o conector indisponivel
    ate a proxima venda (o RemoteStart volta a disponibilizar)."""
    if _tem_recarga_aberta(db, carregador):
        return
    momento = agora()
    anterior = _ultimo_bloqueio.get(carregador.id_hardware)
    if anterior and momento - anterior < INTERVALO_BLOQUEIO:
        return
    _ultimo_bloqueio[carregador.id_hardware] = momento
    registrar_historico(
        db,
        carregador.id_hardware,
        "SESSAO",
        f"ALERTA: carregando sem recarga paga ({motivo})"
        + (". Mandando parar." if settings.BLOQUEAR_CARGA_SEM_PAGAMENTO else ". Bloqueio desligado (BLOQUEAR_CARGA_SEM_PAGAMENTO=false)."),
    )
    if not settings.BLOQUEAR_CARGA_SEM_PAGAMENTO:
        return

    carregador_id, conector, tx = carregador.id_hardware, carregador.connector_id, transaction_id or 0

    def parar():
        # Fora do tratamento da mensagem: o carregador recebe nossa resposta antes.
        from app.services import ocpp_server  # ocpp_server importa este modulo

        ocpp_server.enviar_chamada(carregador_id, "RemoteStopTransaction", {"transactionId": tx})
        ocpp_server.enviar_chamada(carregador_id, "ChangeAvailability", {"connectorId": conector, "type": "Inoperative"})

    threading.Timer(1.0, parar).start()


def _status_notification(db, carregador: Carregador, payload: dict) -> dict:
    connector_id = _inteiro(payload.get("connectorId"))
    status = payload.get("status")
    erro = payload.get("errorCode")
    # connectorId 0 e' o carregador inteiro; so guardamos o conector que vendemos.
    if connector_id == carregador.connector_id:
        erro_gravado = None if erro in (None, "NoError") else erro
        if status != carregador.status_conector or erro_gravado != carregador.erro_conector:
            registrar_historico(
                db,
                carregador.id_hardware,
                "DISPOSITIVO",
                f"Conector {connector_id}: {status}" + (f" (erro {erro})" if erro_gravado else ""),
            )
        carregador.status_conector = status
        carregador.erro_conector = erro_gravado
        if status in STATUS_ENTREGANDO:
            _bloquear_carga_sem_pagamento(db, carregador, None, f"conector em {status}")
    elif connector_id == 0 and status == "Faulted":
        registrar_historico(db, carregador.id_hardware, "DISPOSITIVO", f"Carregador em falha: {erro}")
    return {}


def _sessao_esperando_tag(db, carregador: Carregador, id_tag: str) -> SessaoCarga | None:
    return (
        db.query(SessaoCarga)
        .filter(
            SessaoCarga.carregador_id == carregador.id_hardware,
            SessaoCarga.id_tag == id_tag,
            SessaoCarga.status.in_(STATUS_SESSAO_ANTES_DE_CARREGAR),
        )
        .order_by(SessaoCarga.id.desc())
        .first()
    )


def _authorize(db, carregador: Carregador, payload: dict) -> dict:
    id_tag = str(payload.get("idTag") or "")
    aceita = id_tag in tags_mestre() or _sessao_esperando_tag(db, carregador, id_tag) is not None
    if not aceita:
        registrar_historico(db, carregador.id_hardware, "SESSAO", f"Authorize recusado para idTag={id_tag}")
    return {"idTagInfo": {"status": "Accepted" if aceita else "Invalid"}}


def _start_transaction(db, carregador: Carregador, payload: dict) -> dict:
    id_tag = str(payload.get("idTag") or "")
    meter_start = _inteiro(payload.get("meterStart"))

    # Reenvio do mesmo StartTransaction (a resposta anterior se perdeu): devolve
    # o mesmo transactionId em vez de abrir outra sessao.
    repetida = (
        db.query(SessaoCarga)
        .filter(
            SessaoCarga.carregador_id == carregador.id_hardware,
            SessaoCarga.id_tag == id_tag,
            SessaoCarga.status == "carregando",
        )
        .order_by(SessaoCarga.id.desc())
        .first()
    )
    if repetida is not None:
        return {"transactionId": repetida.id, "idTagInfo": {"status": "Accepted"}}

    sessao = _sessao_esperando_tag(db, carregador, id_tag)
    if sessao is None and id_tag in tags_mestre():
        sessao = _abrir_sessao_tag_mestre(db, carregador, id_tag)
    if sessao is None:
        registrar_historico(db, carregador.id_hardware, "SESSAO", f"StartTransaction recusado para idTag={id_tag}")
        # O OCPP exige um transactionId mesmo recusando; o carregador deve parar com Invalid.
        return {"transactionId": 0, "idTagInfo": {"status": "Invalid"}}

    inicio = agora()
    campos = {
        "status": "carregando",
        "inicio": inicio,
        "connector_id": _inteiro(payload.get("connectorId")) or sessao.connector_id,
        "meter_inicio_wh": meter_start,
        "fim_previsto": inicio + timedelta(minutes=sessao.minutos_pagos) if sessao.minutos_pagos > 0 else None,
    }
    # UPDATE condicional: se o worker acabou de dar a sessao como falha (e
    # estornar), a carga nao pode comecar.
    alterou = (
        db.query(SessaoCarga)
        .filter(SessaoCarga.id == sessao.id, SessaoCarga.status.in_(STATUS_SESSAO_ANTES_DE_CARREGAR))
        .update(campos, synchronize_session=False)
    )
    if alterou != 1:
        return {"transactionId": 0, "idTagInfo": {"status": "Invalid"}}
    db.refresh(sessao)
    registrar_historico(
        db,
        carregador.id_hardware,
        "SESSAO",
        f"Sessao {sessao.id} comecou a carregar ({sessao.minutos_pagos} min pagos)",
    )
    return {"transactionId": sessao.id, "idTagInfo": {"status": "Accepted"}}


def _abrir_sessao_tag_mestre(db, carregador: Carregador, id_tag: str) -> SessaoCarga:
    """Tag RFID mestre (manutencao): sessao sem limite de tempo e sem pagamento."""
    cobranca = Cobranca(
        carregador_id=carregador.id_hardware,
        minutos=0,
        valor=0,
        origem="tag_mestre",
        status="aprovada",
        external_reference=f"EV:{carregador.id_hardware}:tag:{agora().timestamp():.0f}:{id_tag}",
        pago_em=agora(),
    )
    db.add(cobranca)
    db.flush()
    sessao = SessaoCarga(
        carregador_id=carregador.id_hardware,
        cobranca_id=cobranca.id,
        connector_id=carregador.connector_id,
        id_tag=id_tag,
        minutos_pagos=0,
        status="aguardando_carro",
    )
    db.add(sessao)
    db.flush()
    return sessao


def _stop_transaction(db, carregador: Carregador, payload: dict) -> dict:
    transaction_id = _inteiro(payload.get("transactionId"))
    sessao = db.get(SessaoCarga, transaction_id) if transaction_id is not None else None
    if sessao is None or sessao.carregador_id != carregador.id_hardware:
        registrar_historico(db, carregador.id_hardware, "SESSAO", f"StopTransaction de transacao desconhecida ({transaction_id})")
        return {}
    if sessao.status in ("finalizada", "falhou"):
        return {}  # reenvio
    reason = payload.get("reason") or "Local"
    sessao.status = "finalizada"
    sessao.fim_real = agora()
    sessao.meter_fim_wh = _inteiro(payload.get("meterStop"))
    if sessao.meter_fim_wh is not None:
        sessao.meter_atual_wh = sessao.meter_fim_wh
    sessao.motivo_fim = sessao.motivo_fim or MOTIVOS_PARADA.get(reason, reason)
    registrar_historico(
        db,
        carregador.id_hardware,
        "SESSAO",
        f"Sessao {sessao.id} encerrada ({sessao.motivo_fim}), energia {energia_kwh(sessao):.2f} kWh",
    )
    return {}


def _meter_values(db, carregador: Carregador, payload: dict) -> dict:
    transaction_id = _inteiro(payload.get("transactionId"))
    sessao = db.get(SessaoCarga, transaction_id) if transaction_id is not None else None
    if sessao is None or sessao.carregador_id != carregador.id_hardware:
        if _corrente(payload) > 0.5:
            _bloquear_carga_sem_pagamento(db, carregador, transaction_id, f"medindo {_corrente(payload):.1f} A, transactionId {transaction_id}")
        return {}
    for meter_value in payload.get("meterValue") or []:
        for amostra in meter_value.get("sampledValue") or []:
            # Sem measurand, o padrao do OCPP e' Energy.Active.Import.Register.
            if amostra.get("measurand", "Energy.Active.Import.Register") != "Energy.Active.Import.Register":
                continue
            try:
                valor = float(amostra.get("value"))
            except (TypeError, ValueError):
                continue
            if amostra.get("unit") == "kWh":
                valor *= 1000
            sessao.meter_atual_wh = int(valor)
    return {}


def _corrente(payload: dict) -> float:
    """Maior corrente (A) informada no MeterValues; 0 se nao veio."""
    maior = 0.0
    for meter_value in payload.get("meterValue") or []:
        for amostra in meter_value.get("sampledValue") or []:
            if str(amostra.get("measurand") or "").startswith("Current"):
                try:
                    maior = max(maior, float(amostra.get("value")))
                except (TypeError, ValueError):
                    pass
    return maior


def _ok_vazio(db, carregador: Carregador, payload: dict) -> dict:
    return {}


def _data_transfer(db, carregador: Carregador, payload: dict) -> dict:
    return {"status": "UnknownVendorId"}


def energia_kwh(sessao: SessaoCarga) -> float:
    if sessao.meter_inicio_wh is None or sessao.meter_atual_wh is None:
        return 0.0
    return max(0, sessao.meter_atual_wh - sessao.meter_inicio_wh) / 1000


_TRATADORES = {
    "BootNotification": _boot_notification,
    "Heartbeat": _heartbeat,
    "StatusNotification": _status_notification,
    "Authorize": _authorize,
    "StartTransaction": _start_transaction,
    "StopTransaction": _stop_transaction,
    "MeterValues": _meter_values,
    "DataTransfer": _data_transfer,
    "DiagnosticsStatusNotification": _ok_vazio,
    "FirmwareStatusNotification": _ok_vazio,
}
