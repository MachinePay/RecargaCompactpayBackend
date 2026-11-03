"""Cobranca na Point Smart pela Orders API do Mercado Pago (mesmo formato de
POST /pagamentos/terminal/cobrar da CompactPay: order type "point" mandada
para o terminal_id).

O tablet NAO e' fonte de verdade: o pagamento so conta depois de reconsultar
a order no Mercado Pago (webhook ou poller), com valor conferido. A trava +
checagem de status garante que uma cobranca libera no maximo uma sessao.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal, eh_postgres
from app.models.carregador import Carregador, Cobranca, PacoteTempo, agora
from app.services.ocpp_handlers import registrar_historico

MP_API = "https://api.mercadopago.com"
# Status da order do MP que nao voltam mais para "paga".
STATUS_ORDER_FINAIS = {"canceled": "cancelada", "expired": "expirada", "failed": "recusada", "refunded": "cancelada"}
# Pagamento aprovado depois de cancelada/expirada (a Point ja estava cobrando)
# ainda libera a carga - ou estorna, se o carregador estiver ocupado.
STATUS_QUE_ACEITAM_APROVACAO = ("aguardando_pagamento", "expirada", "cancelada")

# Trava em memoria (uma instancia so, ver ocpp_server.py); no Postgres soma
# com pg_advisory_xact_lock igual o _acquire_payment_lock da CompactPay.
_trava_aprovacao = threading.Lock()


def mp_request(method: str, url: str, token: str, body: dict | None = None, headers: dict | None = None, timeout: float = 30):
    req_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if headers:
        req_headers.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore")
        print(f"[Mercado Pago] {method} {url} falhou ({exc.code}): {error_body or 'erro sem detalhe'}")
        raise HTTPException(status_code=502, detail=f"Mercado Pago recusou ({exc.code})") from exc


def token_do_carregador(carregador: Carregador) -> str:
    return (carregador.mp_access_token or settings.MP_ACCESS_TOKEN or "").strip()


def _usa_mercado_pago(cobranca: Cobranca) -> bool:
    return cobranca.origem == "point"


def criar_cobranca(db, carregador: Carregador, pacote: PacoteTempo, origem: str = "point") -> Cobranca:
    if origem == "point" and settings.MP_SIMULADO:
        origem = "simulado"
    cobranca = Cobranca(
        carregador_id=carregador.id_hardware,
        pacote_id=pacote.id if pacote else None,
        minutos=pacote.minutos,
        valor=pacote.valor,
        origem=origem,
        external_reference=f"EV:{carregador.id_hardware}:{uuid4().hex[:12]}",
    )
    if origem == "point":
        token = token_do_carregador(carregador)
        if not token:
            raise HTTPException(status_code=422, detail="Carregador sem MP_ACCESS_TOKEN")
        if not carregador.mp_terminal_id:
            raise HTTPException(status_code=422, detail="Carregador sem Point vinculada (mp_terminal_id)")
        order = mp_request(
            "POST",
            f"{MP_API}/v1/orders",
            token,
            body={
                "type": "point",
                "external_reference": cobranca.external_reference,
                "description": f"Recarga {pacote.minutos} min - {carregador.nome_local}",
                "transactions": {"payments": [{"amount": f"{pacote.valor:.2f}"}]},
                "config": {"point": {"terminal_id": carregador.mp_terminal_id, "print_on_terminal": "no_ticket"}},
            },
            headers={"X-Idempotency-Key": cobranca.external_reference},
        )
        cobranca.mp_order_id = str(order.get("id") or "") or None
    db.add(cobranca)
    registrar_historico(
        db,
        carregador.id_hardware,
        "PAGAMENTO",
        f"Cobranca de {pacote.minutos} min criada ({origem}, order={cobranca.mp_order_id})",
        valor=pacote.valor,
    )
    db.commit()
    db.refresh(cobranca)
    return cobranca


def aprovar_cobranca(db, cobranca_id: int, payment_id: str | None = None, metodo: str | None = None) -> bool:
    """Marca a cobranca como paga e libera a carga. Idempotente: chamar de novo
    (webhook repetido + poller) nao abre outra sessao."""
    from app.services import sessoes  # import local: sessoes importa este modulo

    with _trava_aprovacao:
        if eh_postgres():
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"ev_cobranca_{cobranca_id}"})
        cobranca = db.get(Cobranca, cobranca_id)
        if cobranca is not None:
            db.refresh(cobranca)  # outra thread pode ter aprovado agora
        if cobranca is None or cobranca.status not in STATUS_QUE_ACEITAM_APROVACAO:
            db.rollback()
            return False
        cobranca.status = "aprovada"
        cobranca.pago_em = agora()
        cobranca.mp_payment_id = payment_id
        cobranca.metodo = metodo
        registrar_historico(
            db,
            cobranca.carregador_id,
            "PAGAMENTO",
            f"Pagamento aprovado (cobranca {cobranca.id}, {metodo or cobranca.origem})",
            valor=cobranca.valor,
        )
        db.commit()
    sessoes.iniciar_sessao_para_cobranca(db, cobranca)
    return True


def consultar_order(db, cobranca: Cobranca) -> None:
    """Reconsulta a order no MP e aplica o resultado na cobranca."""
    if not _usa_mercado_pago(cobranca) or not cobranca.mp_order_id:
        return
    carregador = db.get(Carregador, cobranca.carregador_id)
    order = mp_request("GET", f"{MP_API}/v1/orders/{cobranca.mp_order_id}", token_do_carregador(carregador))
    status = order.get("status")
    cobranca.detalhe_status = f"{status}/{order.get('status_detail')}"
    if status == "processed":
        pagamentos = (order.get("transactions") or {}).get("payments") or [{}]
        pago = pagamentos[0]
        try:
            valor_pago = float(order.get("total_paid_amount") or pago.get("paid_amount") or pago.get("amount") or 0)
        except (TypeError, ValueError):
            valor_pago = 0
        if abs(valor_pago - cobranca.valor) > 0.009:
            registrar_historico(
                db,
                cobranca.carregador_id,
                "PAGAMENTO",
                f"ATENCAO: order {cobranca.mp_order_id} paga com valor {valor_pago} diferente de {cobranca.valor}; nao liberado",
            )
            db.commit()
            return
        metodo = (pago.get("payment_method") or {}).get("type") or (pago.get("payment_method") or {}).get("id")
        db.commit()
        aprovar_cobranca(db, cobranca.id, payment_id=str(pago.get("id") or ""), metodo=metodo)
    elif status in STATUS_ORDER_FINAIS and cobranca.status == "aguardando_pagamento":
        cobranca.status = STATUS_ORDER_FINAIS[status]
        db.commit()
    else:
        db.commit()


def processar_webhook(dados: dict) -> None:
    """Notificacao de order do MP ({"type": "order", "data": {"id": ...}}).
    So o id e' usado; o resto vem da reconsulta."""
    order_id = str(((dados or {}).get("data") or {}).get("id") or "")
    if not order_id:
        return
    db = SessionLocal()
    try:
        cobranca = db.query(Cobranca).filter(Cobranca.mp_order_id == order_id).first()
        if cobranca is None:
            logging.info("[MP webhook] order %s nao e' de carregador, ignorada", order_id)
            return
        consultar_order(db, cobranca)
    finally:
        db.close()


def cancelar_cobranca(db, cobranca: Cobranca) -> bool:
    """Cancela uma cobranca que ainda nao foi paga. Na Point, so da para
    cancelar order que ainda nao esta sendo paga; se o MP recusar, mantem
    aguardando (o poller descobre se foi paga)."""
    if cobranca.status != "aguardando_pagamento":
        return False
    if _usa_mercado_pago(cobranca) and cobranca.mp_order_id:
        carregador = db.get(Carregador, cobranca.carregador_id)
        try:
            mp_request(
                "POST",
                f"{MP_API}/v1/orders/{cobranca.mp_order_id}/cancel",
                token_do_carregador(carregador),
                headers={"X-Idempotency-Key": f"cancel-{cobranca.external_reference}"},
            )
        except HTTPException:
            return False
    cobranca.status = "cancelada"
    db.commit()
    return True


def estornar_cobranca(db, cobranca: Cobranca | None) -> bool:
    if cobranca is None or cobranca.estornada_em or cobranca.status != "aprovada" or cobranca.valor <= 0:
        return False
    if _usa_mercado_pago(cobranca):
        carregador = db.get(Carregador, cobranca.carregador_id)
        try:
            mp_request(
                "POST",
                f"{MP_API}/v1/orders/{cobranca.mp_order_id}/refund",
                token_do_carregador(carregador),
                headers={"X-Idempotency-Key": f"refund-{cobranca.external_reference}"},
            )
        except Exception as exc:
            cobranca.estorno_erro = str(exc)[:500]
            registrar_historico(
                db,
                cobranca.carregador_id,
                "PAGAMENTO",
                f"ESTORNO FALHOU na cobranca {cobranca.id} (order {cobranca.mp_order_id}) - estornar manualmente no Mercado Pago",
                valor=cobranca.valor,
            )
            db.commit()
            return False
    cobranca.estornada_em = agora()
    registrar_historico(db, cobranca.carregador_id, "PAGAMENTO", f"Cobranca {cobranca.id} estornada", valor=cobranca.valor)
    db.commit()
    return True


# ---- Poller (rede de seguranca para webhook perdido + expiracao) --------

def processar_cobrancas_pendentes_uma_vez() -> None:
    db = SessionLocal()
    try:
        limite = agora() - timedelta(seconds=settings.COBRANCA_EXPIRA_SEGUNDOS)
        for cobranca in db.query(Cobranca).filter(Cobranca.status == "aguardando_pagamento").all():
            try:
                consultar_order(db, cobranca)
                db.refresh(cobranca)
                if cobranca.status == "aguardando_pagamento" and cobranca.created_at <= limite:
                    if cancelar_cobranca(db, cobranca):
                        cobranca.status = "expirada"
                        db.commit()
            except Exception:
                db.rollback()
                logging.exception("Erro consultando cobranca %s", cobranca.id)
    finally:
        db.close()


def run_cobrancas_poller() -> None:
    logging.info("Poller de cobrancas iniciado")
    while True:
        try:
            processar_cobrancas_pendentes_uma_vez()
        except Exception:
            logging.exception("Erro no poller de cobrancas")
        time.sleep(settings.COBRANCAS_POLLER_INTERVAL_SECONDS)


def start_cobrancas_poller() -> threading.Thread | None:
    if not settings.START_COBRANCAS_POLLER_WORKER:
        logging.info("Poller de cobrancas desativado por START_COBRANCAS_POLLER_WORKER=false")
        return None
    thread = threading.Thread(target=run_cobrancas_poller, daemon=True)
    thread.start()
    return thread
