"""Recarga por kWh pelo app, com carteira.

O motorista (logado) escolhe os kWh. O valor separado para a recarga e' o
custo da energia mais a margem do carregador (ex.: 80% -> 10 kWh a R$ 2,00
separa R$ 25). O saldo da carteira paga primeiro; Pix ou cartao pagam so o
que faltar. No fim, o servidor cobra o que foi usado e a sobra volta para a
carteira (cobranca_kwh.acertar -> carteira.creditar_sobra).
"""
import logging
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException

from app.core.config import settings
from app.models.carregador import Carregador, Cobranca, Motorista, agora
from app.services import carteira, cobranca_kwh, pagamentos, pix
from app.services.ocpp_handlers import registrar_historico

# Menor valor que vale cobrar no banco: abaixo disso arredonda para cima
# (o que passar fica na carteira).
MINIMO_BANCO = 1.00
# Cartao "em analise" no MP: espera ate isso antes de dar por vencido.
CARTAO_ANALISE_MINUTOS = 30

MENSAGENS_CARTAO = {
    "cc_rejected_insufficient_amount": "Saldo ou limite insuficiente no cartao.",
    "cc_rejected_bad_filled_security_code": "Codigo de seguranca (CVV) incorreto.",
    "cc_rejected_bad_filled_date": "Data de validade incorreta.",
    "cc_rejected_bad_filled_card_number": "Numero do cartao incorreto.",
    "cc_rejected_bad_filled_other": "Confira os dados do cartao.",
    "cc_rejected_call_for_authorize": "Seu banco pediu para autorizar este pagamento. Fale com o banco e tente de novo.",
    "cc_rejected_card_disabled": "Este cartao esta desabilitado. Fale com o banco ou use outro.",
    "cc_rejected_duplicated_payment": "Voce acabou de fazer um pagamento igual. Confira o seu historico.",
    "cc_rejected_high_risk": "Pagamento recusado por seguranca. Tente outro cartao ou pague com Pix.",
    "cc_rejected_max_attempts": "Muitas tentativas com este cartao. Use outro cartao ou Pix.",
}


def previa(carregador: Carregador, kwh: float, saldo: float) -> dict:
    """Conta que o app mostra antes de pagar (o servidor refaz na hora de pagar)."""
    custo = round(kwh * (carregador.preco_kwh or 0), 2)
    total = cobranca_kwh.valor_por_kwh(carregador, kwh)
    usar = round(min(max(saldo, 0), total), 2)
    a_pagar = round(total - usar, 2)
    if 0 < a_pagar < MINIMO_BANCO:
        # O banco nao cobra centavos: paga R$ 1,00 e o que passar fica na carteira.
        a_pagar = MINIMO_BANCO
        total = round(usar + a_pagar, 2)
    return {
        "kwh": kwh,
        "preco_kwh": carregador.preco_kwh,
        "percentual_entrega": cobranca_kwh.percentual_entrega(carregador),
        "custo_energia": custo,
        "reserva": round(total - custo, 2),
        "total": total,
        "saldo": round(saldo, 2),
        "saldo_usado": usar,
        "a_pagar": a_pagar,
        "saldo_depois_se_usar_tudo": round(saldo - usar, 2),
    }


def _conferir(conta: dict, a_pagar_visto: float | None) -> None:
    if a_pagar_visto is not None and abs(conta["a_pagar"] - a_pagar_visto) > 0.009:
        raise HTTPException(status_code=409, detail="O valor mudou (seu saldo foi atualizado). Confira e tente de novo.")


def criar(
    db, carregador: Carregador, motorista: Motorista, kwh: float, metodo: str, dispositivo: str,
    a_pagar_visto: float | None = None, cartao: dict | None = None,
) -> Cobranca:
    conta = previa(carregador, kwh, carteira.saldo(motorista))
    _conferir(conta, a_pagar_visto)
    if conta["a_pagar"] <= 0:
        return _so_com_saldo(db, carregador, motorista, conta, dispositivo)
    if metodo == "saldo":
        raise HTTPException(status_code=409, detail="Seu saldo nao cobre esta recarga. Escolha Pix ou cartao para completar.")
    if metodo == "pix":
        cobranca = pix.criar_cobranca_pix(
            db, carregador, 0, dispositivo, motorista.id, kwh=kwh, valor_carteira=conta["saldo_usado"], valor_a_pagar=conta["a_pagar"]
        )
    elif metodo == "cartao":
        # O saldo e' separado ANTES de passar o cartao (se o saldo mudou, nao cobra).
        cobranca = _cartao(db, carregador, motorista, conta, dispositivo, cartao or {})
        _aplicar_resultado_cartao(db, cobranca)
        return cobranca
    else:
        raise HTTPException(status_code=422, detail="Forma de pagamento invalida")
    try:
        carteira.reservar(db, cobranca)
    except HTTPException:
        pagamentos.cancelar_cobranca(db, cobranca)  # Pix ainda nao pago
        raise
    return cobranca


def _so_com_saldo(db, carregador: Carregador, motorista: Motorista, conta: dict, dispositivo: str) -> Cobranca:
    cobranca = Cobranca(
        carregador_id=carregador.id_hardware,
        minutos=cobranca_kwh.teto_minutos(carregador),
        kwh=conta["kwh"],
        preco_kwh=carregador.preco_kwh,
        valor=0,
        valor_carteira=conta["saldo_usado"],
        carteira=True,
        origem="carteira",
        metodo="saldo",
        external_reference=f"EV:{carregador.id_hardware}:saldo:{uuid4().hex[:12]}",
        dispositivo_id=dispositivo,
        motorista_id=motorista.id,
    )
    db.add(cobranca)
    db.commit()
    carteira.reservar(db, cobranca)
    registrar_historico(db, carregador.id_hardware, "PAGAMENTO", f"Recarga de {conta['kwh']:g} kWh paga com o saldo da carteira", valor=conta["saldo_usado"])
    db.commit()
    pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=None, metodo="saldo")
    db.refresh(cobranca)
    return cobranca


def _cartao(db, carregador: Carregador, motorista: Motorista, conta: dict, dispositivo: str, cartao: dict) -> Cobranca:
    """Cartao de credito/debito no app (Checkout API do MP). O app manda o
    token do cartao (o numero nunca passa pelo servidor)."""
    simulado = settings.MP_SIMULADO
    cobranca = Cobranca(
        carregador_id=carregador.id_hardware,
        minutos=cobranca_kwh.teto_minutos(carregador),
        kwh=conta["kwh"],
        preco_kwh=carregador.preco_kwh,
        valor=conta["a_pagar"],
        valor_carteira=conta["saldo_usado"] or None,
        carteira=True,
        origem="simulado" if simulado else "cartao_app",
        metodo=str(cartao.get("payment_method_id") or "cartao"),
        external_reference=f"EV:{carregador.id_hardware}:cartao:{uuid4().hex[:12]}",
        dispositivo_id=dispositivo,
        motorista_id=motorista.id,
        pix_expira_em=agora() + timedelta(minutes=CARTAO_ANALISE_MINUTOS),
    )
    token = None if simulado else pagamentos.token_do_carregador(carregador)
    if not simulado and not cartao.get("token"):
        raise HTTPException(status_code=422, detail="Dados do cartao incompletos")
    if not simulado and not token:
        raise HTTPException(status_code=422, detail="Este carregador ainda nao aceita cartao")
    db.add(cobranca)
    db.commit()
    try:
        carteira.reservar(db, cobranca)
    except HTTPException:
        cobranca.status = "cancelada"
        db.commit()
        raise
    if simulado:
        cobranca.detalhe_status = "approved/accredited"
        cobranca.cartao_final = "teste"
    else:
        identificacao = cartao.get("identification") or (cartao.get("payer") or {}).get("identification") or {}
        corpo = {
            "transaction_amount": conta["a_pagar"],
            "token": cartao["token"],
            "description": f"Recarga {conta['kwh']:g} kWh - {carregador.nome_local}"[:200],
            "installments": int(cartao.get("installments") or 1),
            "payment_method_id": cartao.get("payment_method_id"),
            "external_reference": cobranca.external_reference,
            "payer": {"email": (cartao.get("payer") or {}).get("email") or motorista.email or settings.PIX_EMAIL_PAGADOR},
        }
        if cartao.get("issuer_id"):
            corpo["issuer_id"] = cartao["issuer_id"]
        if identificacao.get("number"):
            corpo["payer"]["identification"] = {"type": identificacao.get("type") or "CPF", "number": str(identificacao["number"])}
        try:
            pagamento = pagamentos.mp_request(
                "POST", f"{pagamentos.MP_API}/v1/payments", token, body=corpo, headers={"X-Idempotency-Key": cobranca.external_reference}
            )
        except HTTPException:
            cobranca.status = "recusada"
            db.commit()
            carteira.devolver_reserva(db, cobranca)
            raise HTTPException(status_code=502, detail="Nao foi possivel passar o cartao agora. Tente de novo ou pague com Pix.")
        cobranca.mp_payment_id = str(pagamento.get("id") or "") or None
        cobranca.metodo = pagamento.get("payment_type_id") or cobranca.metodo
        cobranca.detalhe_status = f"{pagamento.get('status')}/{pagamento.get('status_detail')}"
        final = (pagamento.get("card") or {}).get("last_four_digits")
        cobranca.cartao_final = f"{pagamento.get('payment_method_id') or ''} final {final}".strip() if final else None
    registrar_historico(
        db, carregador.id_hardware, "PAGAMENTO",
        f"Cartao no app: {conta['kwh']:g} kWh ({cobranca.detalhe_status}, payment={cobranca.mp_payment_id})", valor=cobranca.valor,
    )
    db.commit()
    db.refresh(cobranca)
    return cobranca


def _aplicar_resultado_cartao(db, cobranca: Cobranca) -> None:
    status, _, detalhe = (cobranca.detalhe_status or "").partition("/")
    if status in ("approved", "authorized"):
        pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=cobranca.mp_payment_id or f"sim-{cobranca.id}", metodo=cobranca.metodo)
    elif status in ("rejected", "cancelled"):
        cobranca.status = "recusada"
        db.commit()
        carteira.devolver_reserva(db, cobranca)
        raise HTTPException(status_code=402, detail=MENSAGENS_CARTAO.get(detalhe, "Pagamento recusado pelo banco. Tente outro cartao ou pague com Pix."))
    else:
        logging.info("Cartao do app %s em analise (%s)", cobranca.id, cobranca.detalhe_status)
    db.refresh(cobranca)


def mensagem_cartao(detalhe_status: str | None) -> str | None:
    _, _, detalhe = (detalhe_status or "").partition("/")
    return MENSAGENS_CARTAO.get(detalhe)
