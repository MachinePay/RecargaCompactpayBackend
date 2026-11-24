"""Recarga por kWh pelo app (Pix ou cartao online), sem carteira.

O motorista escolhe os kWh. Pix: cobra o custo da energia mais a margem do
carregador (ex.: 80% -> 10 kWh a R$ 2,00 cobra R$ 25) e, no fim, devolve a
sobra no proprio Pix (estorno parcial). Cartao: nao tem devolucao, entao a
margem e' pequena (95% -> R$ 21,05) e nada volta. Conta no app e' opcional.

Cobrancas antigas da carteira (cobranca.carteira) continuam sendo acertadas
pela carteira (services/carteira.py); nenhuma nova e' criada.
"""
import logging
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException

from app.core.config import settings
from app.models.carregador import Carregador, Cobranca, Motorista, agora
from app.services import cobranca_kwh, pagamentos, pix
from app.services.ocpp_handlers import registrar_historico

# Menor valor que o banco cobra.
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


def _conta(carregador: Carregador, kwh: float, metodo: str) -> dict:
    custo = round(kwh * (carregador.preco_kwh or 0), 2)
    total = max(cobranca_kwh.valor_por_kwh(carregador, kwh, metodo), MINIMO_BANCO)
    return {"custo_energia": custo, "reserva": round(total - custo, 2), "a_pagar": total, "devolve_sobra": metodo == "pix"}


def previa(carregador: Carregador, kwh: float) -> dict:
    """Conta que o app mostra antes de pagar, para Pix e para cartao (o
    servidor refaz igual na hora de pagar)."""
    pix_ = _conta(carregador, kwh, "pix")
    cartao = _conta(carregador, kwh, "cartao")
    return {
        "kwh": kwh,
        "preco_kwh": carregador.preco_kwh,
        "custo_energia": pix_["custo_energia"],
        "percentual_pix": cobranca_kwh.percentual_entrega(carregador),
        "percentual_cartao": cobranca_kwh.percentual_cartao_maquina(carregador),
        "pix": pix_,
        "cartao": cartao,
        # Compatibilidade: o valor do Pix.
        "reserva": pix_["reserva"],
        "total": pix_["a_pagar"],
        "a_pagar": pix_["a_pagar"],
    }


def criar(
    db, carregador: Carregador, motorista: Motorista | None, kwh: float, metodo: str, dispositivo: str,
    a_pagar_visto: float | None = None, cartao: dict | None = None,
) -> Cobranca:
    conta = {"kwh": kwh, **_conta(carregador, kwh, "pix" if metodo == "pix" else "cartao")}
    if a_pagar_visto is not None and abs(conta["a_pagar"] - a_pagar_visto) > 0.009:
        raise HTTPException(status_code=409, detail="O preco deste carregador mudou. Confira o valor e tente de novo.")
    motorista_id = motorista.id if motorista else None
    if metodo == "pix":
        return pix.criar_cobranca_pix(db, carregador, 0, dispositivo, motorista_id, kwh=kwh)
    if metodo == "cartao":
        cobranca = _cartao(db, carregador, motorista, conta, dispositivo, cartao or {})
        _aplicar_resultado_cartao(db, cobranca)
        return cobranca
    raise HTTPException(status_code=422, detail="Forma de pagamento invalida")


def _cartao(db, carregador: Carregador, motorista: Motorista | None, conta: dict, dispositivo: str, cartao: dict) -> Cobranca:
    """Cartao de credito/debito no app (Checkout API do MP). O app manda o
    token do cartao (o numero nunca passa pelo servidor)."""
    simulado = settings.MP_SIMULADO
    token = None if simulado else pagamentos.token_do_carregador(carregador)
    if not simulado and not cartao.get("token"):
        raise HTTPException(status_code=422, detail="Dados do cartao incompletos")
    if not simulado and not token:
        raise HTTPException(status_code=422, detail="Este carregador ainda nao aceita cartao")
    cobranca = Cobranca(
        carregador_id=carregador.id_hardware,
        minutos=cobranca_kwh.teto_minutos(carregador),
        kwh=conta["kwh"],
        preco_kwh=carregador.preco_kwh,
        valor=conta["a_pagar"],
        origem="simulado" if simulado else "cartao_app",
        metodo=str(cartao.get("payment_method_id") or "cartao"),
        external_reference=f"EV:{carregador.id_hardware}:cartao:{uuid4().hex[:12]}",
        dispositivo_id=dispositivo,
        motorista_id=motorista.id if motorista else None,
        pix_expira_em=agora() + timedelta(minutes=CARTAO_ANALISE_MINUTOS),
    )
    if simulado:
        cobranca.detalhe_status = "approved/accredited"
        cobranca.cartao_final = "teste"
    else:
        identificacao = cartao.get("identification") or (cartao.get("payer") or {}).get("identification") or {}
        email = (cartao.get("payer") or {}).get("email") or (motorista.email if motorista else None) or settings.PIX_EMAIL_PAGADOR
        corpo = {
            "transaction_amount": conta["a_pagar"],
            "token": cartao["token"],
            "description": f"Recarga {conta['kwh']:g} kWh - {carregador.nome_local}"[:200],
            "installments": int(cartao.get("installments") or 1),
            "payment_method_id": cartao.get("payment_method_id"),
            "external_reference": cobranca.external_reference,
            "payer": {"email": email},
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
            raise HTTPException(status_code=502, detail="Nao foi possivel passar o cartao agora. Tente de novo ou pague com Pix.")
        cobranca.mp_payment_id = str(pagamento.get("id") or "") or None
        cobranca.metodo = pagamento.get("payment_type_id") or cobranca.metodo
        cobranca.detalhe_status = f"{pagamento.get('status')}/{pagamento.get('status_detail')}"
        final = (pagamento.get("card") or {}).get("last_four_digits")
        cobranca.cartao_final = f"{pagamento.get('payment_method_id') or ''} final {final}".strip() if final else None
    db.add(cobranca)
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
        raise HTTPException(status_code=402, detail=MENSAGENS_CARTAO.get(detalhe, "Pagamento recusado pelo banco. Tente outro cartao ou pague com Pix."))
    else:
        logging.info("Cartao do app %s em analise (%s)", cobranca.id, cobranca.detalhe_status)
    db.refresh(cobranca)


def mensagem_cartao(detalhe_status: str | None) -> str | None:
    _, _, detalhe = (detalhe_status or "").partition("/")
    return MENSAGENS_CARTAO.get(detalhe)
