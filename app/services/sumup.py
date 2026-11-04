"""SumUp, igual a CompactPay (services/sumup.py + sumup_poller.py de la):
o cliente cadastra a API key, a maquininha e' pareada pelo codigo que ela
mostra, e o poller le o historico de transacoes de cada cliente e libera o
carregador da maquininha que recebeu o pagamento.

Pagamento digitado direto na maquininha nao gera webhook (so checkout criado
pela API gera), por isso o poller. O carregador sai do serial do reader no
recibo (card_reader.code); Pix nao traz serial e casa pelo operador logado
na maquininha (sumup_usuario).
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import or_

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cliente, Cobranca, agora

API = "https://api.sumup.com"
STATUS_PAGO = {"successful", "success", "paid"}
# Margem no changes_since: transacao que muda de status logo depois da ultima
# leitura nao escapa. Repetida nao credita em dobro (external_reference unico).
MARGEM_SINCRONIA = timedelta(minutes=5)


def su_request(method: str, url: str, api_key: str, body: dict | None = None):
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        corpo = exc.read().decode("utf-8", errors="ignore")
        print(f"[SumUp] {method} {url} falhou ({exc.code}): {corpo or 'erro sem detalhe'}")
        try:
            dados = json.loads(corpo) if corpo else {}
        except ValueError:
            dados = {"message": corpo}
        mensagem = str(dados.get("message") or dados.get("error_message") or dados.get("error") or "sem detalhe") if isinstance(dados, dict) else corpo
        raise HTTPException(status_code=502, detail=f"SumUp retornou erro {exc.code}: {mensagem}") from exc


def merchant_code(api_key: str) -> str:
    """Valida a API key e devolve o merchant_code da conta (GET /v0.1/me)."""
    codigo = (su_request("GET", f"{API}/v0.1/me", api_key).get("merchant_profile") or {}).get("merchant_code")
    if not codigo:
        raise HTTPException(status_code=502, detail="SumUp nao retornou o merchant_code desta API key")
    return str(codigo)


def listar_readers(api_key: str, merchant: str) -> list[dict]:
    dados = su_request("GET", f"{API}/v0.1/merchants/{merchant}/readers", api_key)
    itens = dados if isinstance(dados, list) else (dados.get("items") or dados.get("readers") or dados.get("data") or [])
    readers = []
    for item in itens:
        serial = (item.get("device") or {}).get("identifier")
        readers.append({"id": item.get("id") or item.get("reader_id"), "nome": item.get("name") or serial, "serial": serial, "status": item.get("status")})
    return readers


def parear_reader(api_key: str, merchant: str, codigo: str, nome: str | None = None) -> dict:
    corpo = {"pairing_code": codigo.strip()}
    if nome:
        corpo["name"] = nome.strip()
    dados = su_request("POST", f"{API}/v0.1/merchants/{merchant}/readers", api_key, body=corpo)
    if not dados.get("id"):
        raise HTTPException(status_code=502, detail="SumUp nao retornou o id da maquininha pareada")
    serial = (dados.get("device") or {}).get("identifier")
    return {"id": dados["id"], "nome": dados.get("name") or serial, "serial": serial, "status": dados.get("status")}


def estornar(api_key: str, transaction_id: str) -> None:
    su_request("POST", f"{API}/v0.1/me/refund/{urllib.parse.quote(transaction_id)}", api_key, body={})


def _historico(api_key: str, merchant: str, desde: str | None) -> list[dict]:
    # Sem filtro de payment_types: na CompactPay o "POS" zerava o resultado.
    params = {"statuses[]": "SUCCESSFUL", "limit": "100", "order": "ascending"}
    if desde:
        params["changes_since"] = desde
    dados = su_request("GET", f"{API}/v2.1/merchants/{merchant}/transactions/history?{urllib.parse.urlencode(params)}", api_key)
    return (dados.get("items") if isinstance(dados, dict) else dados) or []


def _serial_do_recibo(api_key: str, merchant: str, transaction_id: str) -> str | None:
    """Unico lugar que traz o serial do reader numa venda direto na maquininha."""
    recibo = su_request("GET", f"{API}/v1.1/receipts/{urllib.parse.quote(transaction_id)}?{urllib.parse.urlencode({'mid': merchant})}", api_key)
    codigo = (((recibo or {}).get("transaction_data") or {}).get("card_reader") or {}).get("code")
    return str(codigo) if codigo else None


def _carregador_da_transacao(serial, usuario, por_serial, por_reader, mapa_reader, por_usuario) -> tuple[Carregador | None, str]:
    """Mesma ordem do _resolve_maquina da CompactPay; nunca credita no escuro."""
    if serial:
        if serial in por_serial:
            return por_serial[serial], "serial"
        reader_id = mapa_reader.get(serial)
        if reader_id and reader_id in por_reader:
            return por_reader[reader_id], "reader"
        return None, "maquininha fora do sistema"
    usuario = (usuario or "").strip().lower()
    if usuario and usuario in por_usuario:
        return por_usuario[usuario], "operador"
    return None, "sem serial e operador nao confere"


def processar_cliente(db, cliente: Cliente) -> None:
    from app.services import mercado_pago_caixa  # importa pagamentos, que importa sessoes

    api_key = (cliente.sumup_api_key or "").strip()
    merchant = (cliente.sumup_merchant_code or "").strip()
    if not api_key or not merchant:
        return
    carregadores = (
        db.query(Carregador)
        .filter(
            Carregador.cliente_id == cliente.id,
            Carregador.banco_pagamento == "sumup",
            Carregador.excluido_em.is_(None),
            or_(Carregador.sumup_reader_id.isnot(None), Carregador.sumup_device_code.isnot(None), Carregador.sumup_usuario.isnot(None)),
        )
        .all()
    )
    if not carregadores:
        return
    por_reader = {c.sumup_reader_id: c for c in carregadores if c.sumup_reader_id}
    por_serial = {c.sumup_device_code: c for c in carregadores if c.sumup_device_code}
    por_usuario = {c.sumup_usuario.strip().lower(): c for c in carregadores if c.sumup_usuario}

    inicio_leitura = agora()
    desde = (cliente.sumup_last_sync_at - MARGEM_SINCRONIA).strftime("%Y-%m-%dT%H:%M:%SZ") if cliente.sumup_last_sync_at else None
    try:
        transacoes = _historico(api_key, merchant, desde)
    except HTTPException as exc:
        logging.warning("[SumUp] historico do cliente %s falhou: %s", cliente.id, exc.detail)
        return
    try:
        mapa_reader = {r["serial"]: str(r["id"]) for r in listar_readers(api_key, merchant) if r["serial"] and str(r["id"]) in por_reader}
    except HTTPException:
        mapa_reader = {}

    for transacao in transacoes:
        if str(transacao.get("status") or "").strip().lower() not in STATUS_PAGO:
            continue
        transaction_id = str(transacao.get("id") or transacao.get("transaction_id") or "").strip()
        if not transaction_id or db.query(Cobranca.id).filter(Cobranca.mp_payment_id == transaction_id, Cobranca.origem == "sumup").first():
            continue
        try:
            valor = float(transacao.get("amount"))
        except (TypeError, ValueError):
            continue
        try:
            serial = _serial_do_recibo(api_key, merchant, transaction_id)
        except HTTPException:
            serial = None
        carregador, motivo = _carregador_da_transacao(serial, transacao.get("user"), por_serial, por_reader, mapa_reader, por_usuario)
        if carregador is None:
            logging.info("[SumUp] transacao %s (R$ %.2f) ignorada: %s", transaction_id, valor, motivo)
            continue
        metodo = str(transacao.get("payment_type") or transacao.get("card_type") or "sumup").lower()
        mercado_pago_caixa.registrar_pagamento(db, carregador, valor, transaction_id, metodo, origem="sumup")

    cliente.sumup_last_sync_at = inicio_leitura
    db.commit()


def processar_uma_vez() -> None:
    db = SessionLocal()
    try:
        for cliente in db.query(Cliente).filter(Cliente.ativo.is_(True), Cliente.cliente_sumup.is_(True)).all():
            try:
                processar_cliente(db, cliente)
            except Exception:
                db.rollback()
                logging.exception("[SumUp] erro processando cliente %s", cliente.id)
    finally:
        db.close()


def _rodar() -> None:
    logging.info("Poller SumUp iniciado")
    while True:
        try:
            processar_uma_vez()
        except Exception:
            logging.exception("Erro no poller SumUp")
        time.sleep(settings.SUMUP_POLLER_INTERVAL_SECONDS)


def start_sumup_poller() -> threading.Thread | None:
    if not settings.START_SUMUP_POLLER_WORKER:
        logging.info("Poller SumUp desativado por START_SUMUP_POLLER_WORKER=false")
        return None
    thread = threading.Thread(target=_rodar, daemon=True)
    thread.start()
    return thread
