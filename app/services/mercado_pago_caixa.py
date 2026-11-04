"""Venda pela maquininha vinculada ao caixa do carregador (mesmo modelo da
CompactPay): cada carregador tem uma loja e um caixa (POS) no Mercado Pago;
qualquer pagamento aprovado numa maquininha vinculada a esse caixa libera
tempo de recarga pela regra de preco do carregador (preco_valor reais =
preco_minutos minutos, proporcional).

O aviso chega pelo webhook de payment. O pagamento e' sempre reconsultado no
MP; o carregador sai do pos_id/store_id do pagamento, igual ao
_resolve_machine_by_mp_location da CompactPay.
"""
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionLocal, eh_postgres
from app.models.carregador import Carregador, Cliente, Cobranca, agora
from app.services import pagamentos
from app.services.ocpp_handlers import registrar_historico

STATUS_PAGO = {"approved", "authorized"}

_trava_pagamento = threading.Lock()


# ---- Regra de preco -----------------------------------------------------

def minutos_por_valor(carregador: Carregador, valor: float) -> int:
    """Proporcional e arredondado para baixo: com R$ 10 = 20 min, R$ 7 da 14 min."""
    if not carregador.preco_valor or not carregador.preco_minutos or valor <= 0:
        return 0
    return int(valor * carregador.preco_minutos / carregador.preco_valor + 1e-9)


# ---- Loja e caixa (copiado do fluxo da CompactPay) ----------------------

def _normalizar_external_id(valor: str, max_length: int = 39) -> str:
    normalizado = re.sub(r"[^A-Za-z0-9]", "", valor.upper())
    return (normalizado or f"EV{int(time.time())}")[:max_length]


def _sem_acento(valor: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", valor or "") if unicodedata.category(ch) != "Mn").casefold().strip()


def _categorias(preferida: int | None = None) -> list[int]:
    candidatas = ([preferida] if preferida else []) + [settings.MP_DEFAULT_POS_CATEGORY]
    candidatas += [int(c) for c in settings.MP_POS_CATEGORY_FALLBACKS.split(",") if c.strip().isdigit()]
    return list(dict.fromkeys(candidatas))


def _buscar(url: str, token: str) -> dict | None:
    try:
        resultados = pagamentos.mp_request("GET", url, token).get("results") or []
    except HTTPException:
        return None
    return resultados[0] if resultados else None


def _endereco_da_loja(carregador: Carregador, endereco: dict) -> dict:
    """Endereco informado na hora > endereco do cliente > MP_DEFAULT_STORE_*."""
    cliente = carregador.cliente
    return {
        "street_number": endereco.get("numero") or (cliente and cliente.endereco_numero) or settings.MP_DEFAULT_STORE_STREET_NUMBER,
        "street_name": endereco.get("rua") or (cliente and cliente.endereco_rua) or settings.MP_DEFAULT_STORE_STREET_NAME,
        "city_name": endereco.get("cidade") or (cliente and cliente.endereco_cidade) or settings.MP_DEFAULT_STORE_CITY_NAME,
        "state_name": endereco.get("estado") or (cliente and cliente.endereco_estado) or settings.MP_DEFAULT_STORE_STATE_NAME,
        "latitude": cliente.endereco_latitude if cliente and cliente.endereco_latitude is not None else settings.MP_DEFAULT_STORE_LATITUDE,
        "longitude": cliente.endereco_longitude if cliente and cliente.endereco_longitude is not None else settings.MP_DEFAULT_STORE_LONGITUDE,
    }


def _criar_loja(carregador: Carregador, token: str, endereco: dict) -> dict:
    cliente = carregador.cliente
    user_id = (cliente.mp_user_id if cliente and cliente.mp_access_token and token == cliente.mp_access_token.strip() else None) or (
        carregador.mp_user_id or pagamentos.mp_user_id_do_token(token)
    )
    external_id = _normalizar_external_id(f"EVSTORE{carregador.id_hardware}", max_length=60)
    busca = f"{pagamentos.MP_API}/users/{urllib.parse.quote(user_id)}/stores/search?{urllib.parse.urlencode({'external_id': external_id})}"
    loja = _buscar(busca, token)
    if loja is None:
        nome = carregador.nome_local[:45]
        corpo = {
            "name": nome,
            "external_id": external_id,
            "location": {**_endereco_da_loja(carregador, endereco), "reference": nome},
        }
        url = f"{pagamentos.MP_API}/users/{urllib.parse.quote(user_id)}/stores"
        try:
            loja = pagamentos.mp_request("POST", url, token, body=corpo)
        except HTTPException as exc:
            # O MP so aceita a cidade com a grafia dele e devolve a lista no erro.
            detalhe = str(exc.detail)
            marcador = "Valid values are:"
            if "city_name was invalid" in detalhe and marcador in detalhe:
                validas = [c.strip() for c in detalhe.split(marcador, 1)[1].split(",") if c.strip()]
                certa = next((c for c in validas if _sem_acento(c) == _sem_acento(corpo["location"]["city_name"])), None)
                if not certa:
                    raise HTTPException(status_code=422, detail=f"Cidade '{corpo['location']['city_name']}' nao aceita pelo Mercado Pago") from exc
                corpo["location"]["city_name"] = certa
                loja = pagamentos.mp_request("POST", url, token, body=corpo)
            else:
                loja = _buscar(busca, token)
                if loja is None:
                    raise
    return {"mp_user_id": user_id, "mp_store_id": str(loja.get("id") or ""), "mp_store_external_id": loja.get("external_id") or external_id}


def nomes_no_mp(carregador: Carregador) -> dict:
    """Como a loja e o caixa estao no Mercado Pago agora (para achar no app)."""
    token = pagamentos.token_do_carregador(carregador)
    if not token or not carregador.mp_pos_id:
        return {"loja": None, "caixa": None}
    resposta = {"loja": None, "caixa": None}
    try:
        resposta["loja"] = pagamentos.mp_request("GET", f"{pagamentos.MP_API}/stores/{carregador.mp_store_id}", token).get("name")
    except HTTPException:
        pass
    try:
        resposta["caixa"] = pagamentos.mp_request("GET", f"{pagamentos.MP_API}/pos/{carregador.mp_pos_id}", token).get("name")
    except HTTPException:
        pass
    return resposta


def renomear_no_mp(carregador: Carregador) -> dict:
    """Loja e caixa com o nome do carregador (ex.: "Garagem GiraKids")."""
    token = pagamentos.token_do_carregador(carregador)
    if not token or not carregador.mp_pos_id:
        raise HTTPException(status_code=422, detail="Carregador sem loja e caixa no Mercado Pago")
    pagamentos.mp_request(
        "PUT",
        f"{pagamentos.MP_API}/users/{urllib.parse.quote(str(carregador.mp_user_id))}/stores/{carregador.mp_store_id}",
        token,
        body={"name": carregador.nome_local[:45]},
    )
    pagamentos.mp_request("PUT", f"{pagamentos.MP_API}/pos/{carregador.mp_pos_id}", token, body={"name": carregador.nome_local[:44]})
    return nomes_no_mp(carregador)


def criar_loja_e_caixa(db, carregador: Carregador, endereco: dict | None = None) -> Carregador:
    token = pagamentos.token_do_carregador(carregador)
    if not token:
        raise HTTPException(status_code=422, detail="Sem token do Mercado Pago: vincule o carregador a um cliente com o token cadastrado")
    loja = _criar_loja(carregador, token, endereco or {})
    if not loja["mp_store_id"]:
        raise HTTPException(status_code=502, detail="Mercado Pago criou a loja, mas nao retornou o id")

    external_id = _normalizar_external_id(f"EV{carregador.id_hardware}")
    caixa = _buscar(f"{pagamentos.MP_API}/pos?{urllib.parse.urlencode({'external_id': external_id})}", token)
    ja_existia = caixa is not None
    ultimo_erro = None
    if caixa is None:
        corpo = {"name": carregador.nome_local[:44], "fixed_amount": False, "external_id": external_id}
        if loja["mp_store_id"].isdigit():
            corpo["store_id"] = int(loja["mp_store_id"])
        else:
            corpo["external_store_id"] = loja["mp_store_external_id"]
        cliente = carregador.cliente
        for categoria in _categorias(cliente.mp_pos_category if cliente else None):
            corpo["category"] = categoria
            try:
                caixa = pagamentos.mp_request(
                    "POST", f"{pagamentos.MP_API}/pos", token, body=corpo, headers={"X-Idempotency-Key": f"{external_id}{categoria}"}
                )
                if cliente is not None:
                    cliente.mp_pos_category = categoria
                break
            except HTTPException as exc:
                ultimo_erro = exc
                caixa = _buscar(f"{pagamentos.MP_API}/pos?{urllib.parse.urlencode({'external_id': external_id})}", token)
                if caixa is not None:
                    break
    if caixa is None:
        raise ultimo_erro or HTTPException(status_code=502, detail="Nao foi possivel criar o caixa no Mercado Pago")

    carregador.mp_user_id = loja["mp_user_id"]
    carregador.mp_store_id = loja["mp_store_id"]
    carregador.mp_store_external_id = loja["mp_store_external_id"]
    carregador.mp_pos_id = str(caixa.get("id") or "")
    carregador.mp_pos_external_id = caixa.get("external_id") or external_id
    if ja_existia and caixa.get("name") != carregador.nome_local[:44]:
        # Caixa reaproveitado de um cadastro anterior: fica com o nome atual.
        try:
            renomear_no_mp(carregador)
        except HTTPException:
            logging.warning("[MP caixa] nao renomeou loja/caixa de %s", carregador.id_hardware)
    registrar_historico(
        db,
        carregador.id_hardware,
        "PAGAMENTO",
        f"Loja {carregador.mp_store_id} e caixa {carregador.mp_pos_id} ({carregador.mp_pos_external_id}) no Mercado Pago",
    )
    db.commit()
    return carregador


# ---- Pagamento recebido -------------------------------------------------

def _valores_por_chave(dados, chaves: set[str]) -> set[str]:
    achados = set()
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            if str(chave).lower() in chaves and valor not in (None, ""):
                achados.add(str(valor).strip())
            achados |= _valores_por_chave(valor, chaves)
    elif isinstance(dados, list):
        for item in dados:
            achados |= _valores_por_chave(item, chaves)
    return achados


def _ids_de_local(pagamento: dict) -> dict[str, set[str]]:
    return {
        "store_ids": _valores_por_chave(pagamento, {"store_id"}),
        "store_external_ids": _valores_por_chave(pagamento, {"external_store_id", "store_external_id"}),
        "pos_ids": _valores_por_chave(pagamento, {"pos_id"}),
        "pos_external_ids": _valores_por_chave(pagamento, {"external_pos_id", "pos_external_id"}),
    }


def carregador_do_pagamento(db, pagamento: dict) -> tuple[Carregador | None, str]:
    """Mesma regra da CompactPay: casa por caixa e loja; com os dois no
    pagamento, os dois tem que bater; mais de um carregador = nao libera.
    Sem caixa/loja no pagamento, tenta pelo terminal (maquininha) informado
    no carregador (mp_terminal_id), como a escuta de terminal da CompactPay."""
    ids = _ids_de_local(pagamento)
    tem_caixa = bool(ids["pos_ids"] or ids["pos_external_ids"])
    tem_loja = bool(ids["store_ids"] or ids["store_external_ids"])
    if not tem_caixa and not tem_loja:
        terminais = _valores_por_chave(pagamento, {"terminal_id", "device_id", "serial_number"})
        if terminais:
            por_terminal = (
                db.query(Carregador)
                .filter(Carregador.mp_terminal_id.in_(terminais), Carregador.excluido_em.is_(None))
                .all()
            )
            if len(por_terminal) == 1:
                return por_terminal[0], "ok"
            return None, f"pagamento sem caixa/loja; maquininha {', '.join(sorted(terminais))} nao esta em nenhum carregador"
        return None, "pagamento sem caixa/loja"
    candidatos = []
    for carregador in db.query(Carregador).filter(Carregador.mp_pos_id.isnot(None), Carregador.excluido_em.is_(None)).all():
        caixa = carregador.mp_pos_id in ids["pos_ids"] or (carregador.mp_pos_external_id or "") in ids["pos_external_ids"]
        loja = carregador.mp_store_id in ids["store_ids"] or (carregador.mp_store_external_id or "") in ids["store_external_ids"]
        if (caixa and loja) if (tem_caixa and tem_loja) else (caixa if tem_caixa else loja):
            candidatos.append(carregador)
    if len(candidatos) == 1:
        return candidatos[0], "ok"
    return None, "mais de um carregador com esse caixa" if candidatos else f"nenhum carregador com esse caixa/loja {ids}"


def registrar_pagamento(db, carregador: Carregador, valor: float, payment_id: str, metodo: str | None, origem: str = "caixa") -> Cobranca | None:
    """Libera o tempo de um pagamento aprovado. Idempotente por payment_id.
    - carregador livre: abre a recarga (mesmo caminho da venda do tablet);
    - ja carregando: soma o tempo na recarga em andamento;
    - preparando outra recarga ou sem regra de preco: estorna."""
    from app.services import sessoes  # sessoes importa pagamentos

    # Pagamento da SumUp tem prefixo proprio: o id dela nao pode colidir com um do MP.
    referencia = f"EV:{carregador.id_hardware}:{'sumup' if origem == 'sumup' else 'pay'}:{payment_id}"
    with _trava_pagamento:
        if eh_postgres():
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": referencia})
        if db.query(Cobranca).filter(Cobranca.external_reference == referencia).first():
            db.rollback()
            return None  # webhook repetido (payment.created + payment.updated)
        minutos = minutos_por_valor(carregador, valor)
        cobranca = Cobranca(
            carregador_id=carregador.id_hardware,
            minutos=minutos,
            valor=valor,
            origem=origem,
            external_reference=referencia,
            mp_payment_id=payment_id,
            metodo=metodo,
        )
        db.add(cobranca)
        registrar_historico(
            db, carregador.id_hardware, "PAGAMENTO", f"Pagamento na maquininha {payment_id}: {minutos} min ({metodo or origem})", valor=valor
        )
        db.commit()

    ativa = sessoes.sessao_ativa(db, carregador.id_hardware)
    if minutos < 1 or (ativa is not None and ativa.status != "carregando"):
        motivo = "sem regra de preco (ou valor baixo demais)" if minutos < 1 else "carregador ocupado comecando outra recarga"
        cobranca.status = "aprovada"
        cobranca.pago_em = agora()
        registrar_historico(db, carregador.id_hardware, "PAGAMENTO", f"ATENCAO: pagamento {payment_id} nao liberado: {motivo}; estornando")
        db.commit()
        pagamentos.estornar_cobranca(db, cobranca)
        return cobranca

    if ativa is not None:
        # Pagou de novo com o carro carregando: soma o tempo.
        cobranca.status = "aprovada"
        cobranca.pago_em = agora()
        ativa.minutos_pagos += minutos
        if ativa.fim_previsto is not None:
            ativa.fim_previsto += timedelta(minutes=minutos)
        registrar_historico(db, carregador.id_hardware, "SESSAO", f"Sessao {ativa.id}: +{minutos} min (pagamento {payment_id})")
        db.commit()
        return cobranca

    pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=payment_id, metodo=metodo)
    return cobranca


def _tokens_conhecidos(db, user_id=None) -> list[str]:
    """Primeiro o token do cliente dono da conta que gerou o aviso (o webhook
    manda o user_id, igual resolve_mp_token_by_user_id da CompactPay); depois
    os outros, para aviso sem user_id."""
    preferido = None
    if user_id:
        cliente = db.query(Cliente).filter(Cliente.mp_user_id == str(user_id), Cliente.mp_access_token.isnot(None)).first()
        preferido = cliente.mp_access_token if cliente else None
    tokens = (
        [preferido]
        + [c.mp_access_token for c in db.query(Cliente).filter(Cliente.ativo.is_(True)).all()]
        + [c.mp_access_token for c in db.query(Carregador).all()]
        + [settings.MP_ACCESS_TOKEN]
    )
    return list(dict.fromkeys(t.strip() for t in tokens if t and t.strip()))


def _avisar_nao_reconhecido(pagamento: dict, motivo: str) -> None:
    """Aparece no Diagnostico: pagamento aprovado que nao liberou nada."""
    from app.services import diagnostico

    diagnostico.registrar(
        "pagamento",
        None,
        f"Pagamento {pagamento.get('id')} de R$ {float(pagamento.get('transaction_amount') or 0):.2f} aprovado, mas nao liberou: {motivo}",
        nivel="aviso",
        bruto=json.dumps({k: pagamento.get(k) for k in ("id", "status", "transaction_amount", "pos_id", "store_id", "point_of_interaction", "date_created")}, default=str),
    )


def processar_pagamento(payment_id: str, user_id=None, pagamento: dict | None = None) -> None:
    """Webhook de payment (ou o poller): reconsulta no MP e libera o carregador do caixa."""
    db = SessionLocal()
    try:
        for token in ([] if pagamento is not None else _tokens_conhecidos(db, user_id)):
            try:
                pagamento = pagamentos.mp_request("GET", f"{pagamentos.MP_API}/v1/payments/{payment_id}", token)
                break
            except HTTPException:
                continue
        if pagamento is None:
            logging.warning("[MP caixa] payment %s nao encontrado com nenhum token", payment_id)
            return
        if (pagamento.get("status") or "").lower() not in STATUS_PAGO:
            logging.info("[MP caixa] payment %s ignorado: status %s", payment_id, pagamento.get("status"))
            return
        # Pagamento de order do tablet (Orders API) tambem gera aviso de payment:
        # esse ja e' tratado pela cobranca da order.
        order_id = str((pagamento.get("order") or {}).get("id") or "")
        if str(pagamento.get("external_reference") or "").startswith("EV:") or (
            order_id and db.query(Cobranca).filter(Cobranca.mp_order_id == order_id).first()
        ):
            return
        if db.query(Cobranca.id).filter(Cobranca.mp_payment_id == str(payment_id), Cobranca.origem.in_(("caixa", "point"))).first():
            return  # ja liberado (webhook e poller chegam os dois)
        carregador, motivo = carregador_do_pagamento(db, pagamento)
        if carregador is None:
            logging.info("[MP caixa] payment %s ignorado: %s", payment_id, motivo)
            _avisar_nao_reconhecido(pagamento, motivo)
            return
        metodo = pagamento.get("payment_type_id") or pagamento.get("payment_method_id")
        registrar_pagamento(db, carregador, float(pagamento.get("transaction_amount") or 0), str(payment_id), metodo)
    finally:
        db.close()



# ---- Poller (rede de seguranca do webhook) ------------------------------

# Pagamentos ja olhados pelo poller (liberados ou nao reconhecidos): nao repete
# o aviso a cada volta. Em memoria; o banco segura a duplicidade de verdade.
_vistos: set[str] = set()


def poller_uma_vez() -> None:
    """Le os pagamentos aprovados dos ultimos minutos de cada conta MP que tem
    carregador com caixa e libera os que o webhook nao trouxe."""
    db = SessionLocal()
    try:
        carregadores = db.query(Carregador).filter(Carregador.mp_pos_id.isnot(None), Carregador.excluido_em.is_(None)).all()
        tokens = list(dict.fromkeys(t for t in (pagamentos.token_do_carregador(c) for c in carregadores) if t))
    finally:
        db.close()
    inicio = (agora() - timedelta(minutes=settings.MP_CAIXA_POLLER_JANELA_MINUTOS)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    for token in tokens:
        params = urllib.parse.urlencode(
            {"status": "approved", "sort": "date_created", "criteria": "desc", "limit": 50, "range": "date_created", "begin_date": inicio, "end_date": "NOW"}
        )
        try:
            resultados = pagamentos.mp_request("GET", f"{pagamentos.MP_API}/v1/payments/search?{params}", token).get("results") or []
        except HTTPException:
            logging.warning("[MP caixa] busca de pagamentos falhou para um token")
            continue
        for pagamento in resultados:
            payment_id = str(pagamento.get("id") or "")
            if not payment_id or payment_id in _vistos:
                continue
            _vistos.add(payment_id)
            processar_pagamento(payment_id, pagamento=pagamento)
    if len(_vistos) > 5000:
        _vistos.clear()


def _rodar_poller() -> None:
    logging.info("Poller do caixa Mercado Pago iniciado")
    while True:
        try:
            poller_uma_vez()
        except Exception:
            logging.exception("Erro no poller do caixa Mercado Pago")
        time.sleep(settings.MP_CAIXA_POLLER_INTERVAL_SECONDS)


def start_mp_caixa_poller() -> threading.Thread | None:
    if not settings.START_MP_CAIXA_POLLER:
        logging.info("Poller do caixa MP desativado por START_MP_CAIXA_POLLER=false")
        return None
    thread = threading.Thread(target=_rodar_poller, daemon=True)
    thread.start()
    return thread
