"""Pix do app do motorista: ele le o QR code colado no carregador, escolhe o
tempo e paga um Pix gerado na conta do Mercado Pago do cliente dono.

Mesmo caminho da venda do tablet: a cobranca so e' aprovada depois de
reconsultar o pagamento no MP (webhook de payment ou o poller de cobrancas),
com o valor conferido, e ai pagamentos.aprovar_cobranca libera a carga.
"""
import logging
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException

from app.core.config import settings
from app.models.carregador import Carregador, Cobranca, agora
from app.services import pagamentos
from app.services.ocpp_handlers import registrar_historico

# O MP so aceita vencimento de Pix a partir de 30 min; o nosso prazo menor
# vale pelo cancelamento que o poller faz (PIX_EXPIRA_SEGUNDOS).
VENCIMENTO_MINIMO_MP = timedelta(minutes=30)
STATUS_PAGO = {"approved", "authorized"}
STATUS_FINAIS = {"cancelled": "cancelada", "rejected": "recusada", "refunded": "cancelada", "charged_back": "cancelada"}


def valor_por_minutos(carregador: Carregador, minutos: int) -> float:
    """Inverso de mercado_pago_caixa.minutos_por_valor: R$ 10 = 20 min -> 30 min custa R$ 15."""
    if not carregador.preco_valor or not carregador.preco_minutos or minutos <= 0:
        return 0.0
    return round(minutos * carregador.preco_valor / carregador.preco_minutos, 2)


def _data_mp(momento) -> str:
    return momento.strftime("%Y-%m-%dT%H:%M:%S.000-00:00")


def criar_cobranca_pix(db, carregador: Carregador, minutos: int, dispositivo_id: str) -> Cobranca:
    valor = valor_por_minutos(carregador, minutos)
    if valor < 1:
        raise HTTPException(status_code=422, detail="Este carregador ainda nao tem preco cadastrado")
    origem = "simulado" if settings.MP_SIMULADO else "pix"
    expira_em = agora() + timedelta(seconds=settings.PIX_EXPIRA_SEGUNDOS)
    cobranca = Cobranca(
        carregador_id=carregador.id_hardware,
        minutos=minutos,
        valor=valor,
        origem=origem,
        metodo="pix",
        external_reference=f"EV:{carregador.id_hardware}:pix:{uuid4().hex[:12]}",
        dispositivo_id=dispositivo_id,
        pix_expira_em=expira_em,
    )
    if origem == "pix":
        token = pagamentos.token_do_carregador(carregador)
        if not token:
            raise HTTPException(status_code=422, detail="Este carregador ainda nao aceita Pix")
        pagamento = pagamentos.mp_request(
            "POST",
            f"{pagamentos.MP_API}/v1/payments",
            token,
            body={
                "transaction_amount": valor,
                "description": f"Recarga {minutos} min - {carregador.nome_local}"[:200],
                "payment_method_id": "pix",
                "external_reference": cobranca.external_reference,
                "date_of_expiration": _data_mp(max(expira_em, agora() + VENCIMENTO_MINIMO_MP)),
                "payer": {"email": settings.PIX_EMAIL_PAGADOR},
            },
            headers={"X-Idempotency-Key": cobranca.external_reference},
        )
        dados_pix = (pagamento.get("point_of_interaction") or {}).get("transaction_data") or {}
        cobranca.mp_payment_id = str(pagamento.get("id") or "") or None
        cobranca.pix_copia_cola = dados_pix.get("qr_code")
        if not cobranca.mp_payment_id or not cobranca.pix_copia_cola:
            raise HTTPException(status_code=502, detail="Mercado Pago nao devolveu o Pix")
    else:
        # Bancada: um "copia e cola" de mentira so para a tela ter o que mostrar.
        cobranca.pix_copia_cola = f"00020126SIMULADO{carregador.id_hardware}{uuid4().hex[:16].upper()}5204000053039865802BR6304"
    db.add(cobranca)
    registrar_historico(
        db,
        carregador.id_hardware,
        "PAGAMENTO",
        f"Pix de {minutos} min gerado pelo app do motorista ({origem}, payment={cobranca.mp_payment_id})",
        valor=valor,
    )
    db.commit()
    db.refresh(cobranca)
    return cobranca


def consultar(db, cobranca: Cobranca) -> None:
    """Reconsulta o Pix no MP e aplica o resultado na cobranca."""
    if cobranca.origem != "pix" or not cobranca.mp_payment_id:
        return
    carregador = db.get(Carregador, cobranca.carregador_id)
    pagamento = pagamentos.mp_request(
        "GET", f"{pagamentos.MP_API}/v1/payments/{cobranca.mp_payment_id}", pagamentos.token_do_carregador(carregador)
    )
    status = (pagamento.get("status") or "").lower()
    cobranca.detalhe_status = f"{status}/{pagamento.get('status_detail')}"
    if status in STATUS_PAGO:
        valor_pago = float(pagamento.get("transaction_amount") or 0)
        if abs(valor_pago - cobranca.valor) > 0.009:
            registrar_historico(
                db,
                cobranca.carregador_id,
                "PAGAMENTO",
                f"ATENCAO: Pix {cobranca.mp_payment_id} pago com valor {valor_pago} diferente de {cobranca.valor}; nao liberado",
            )
            db.commit()
            return
        db.commit()
        pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=cobranca.mp_payment_id, metodo="pix")
    elif status in STATUS_FINAIS and cobranca.status == "aguardando_pagamento":
        cobranca.status = STATUS_FINAIS[status]
        db.commit()
    else:
        db.commit()


def cancelar(db, cobranca: Cobranca) -> bool:
    """Cancela o Pix no MP. Se o MP recusar (ja pago), devolve False e o
    poller descobre o pagamento."""
    if cobranca.origem != "pix" or not cobranca.mp_payment_id:
        return True
    carregador = db.get(Carregador, cobranca.carregador_id)
    try:
        pagamentos.mp_request(
            "PUT",
            f"{pagamentos.MP_API}/v1/payments/{cobranca.mp_payment_id}",
            pagamentos.token_do_carregador(carregador),
            body={"status": "cancelled"},
        )
    except HTTPException:
        logging.warning("[Pix] MP nao cancelou o payment %s", cobranca.mp_payment_id)
        return False
    return True
