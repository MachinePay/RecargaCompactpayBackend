"""Cobranca por kWh: para na energia paga e devolve a sobra."""
from app.db.session import SessionLocal
from app.models.carregador import Cobranca, SessaoCarga
from app.services import cobranca_kwh, pagamentos, sessoes
from tests.test_fluxo_carga import ADMIN, CarregadorFake, _esperar, _sessao, cliente_teste, client  # noqa: F401 - fixture


def _carregador_kwh(client, cp_id: str) -> tuple[int, dict]:
    r = client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": cp_id, "nome_local": "Posto kWh"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    # Sem preco do kWh o painel nao deixa trocar de modo.
    r = client.patch(f"/api/v1/admin/carregadores/{cp_id}", json={"modo_cobranca": "kwh"}, headers=ADMIN)
    assert r.status_code == 422
    r = client.patch(f"/api/v1/admin/carregadores/{cp_id}", json={"modo_cobranca": "kwh", "preco_kwh": 2.0}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["modo_cobranca"] == "kwh"
    # Pacote de tempo antigo some do tablet; o de energia aparece.
    client.post(f"/api/v1/admin/carregadores/{cp_id}/pacotes", json={"nome": "30 min", "minutos": 30, "valor": 10}, headers=ADMIN)
    pacote = client.post(f"/api/v1/admin/carregadores/{cp_id}/pacotes", json={"nome": "10 kWh", "kwh": 10, "valor": 20}, headers=ADMIN).json()
    chave = client.post("/api/v1/admin/terminais", json={"nome": f"tablet {cp_id}"}, headers=ADMIN).json()["chave"]
    return pacote["id"], {"X-Terminal-Key": chave}


def _comecar(client, cp, cp_id: str, pacote_id: int, tablet: dict, meter_start: int = 1000) -> int:
    r = client.post("/api/v1/carregador/cobrancas", json={"carregador_id": cp_id, "pacote_id": pacote_id}, headers=tablet)
    assert r.status_code == 200, r.text
    cobranca = client.post(f"/api/v1/carregador/cobrancas/{r.json()['id']}/simular-pagamento", headers=tablet).json()
    assert cobranca["kwh"] == 10 and cobranca["valor"] == 20
    sessao_id = cobranca["sessao"]["id"]
    start = cp.receber_chamada("RemoteStartTransaction")
    cp.responder(start, {"status": "Accepted"})
    cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": meter_start, "timestamp": "2026-11-18T12:00:00Z"})
    return sessao_id


def _medir(cp, sessao_id: int, wh: int) -> None:
    cp.chamar("MeterValues", {"connectorId": 1, "transactionId": sessao_id, "meterValue": [{"timestamp": "2026-11-18T12:10:00Z", "sampledValue": [{"value": str(wh), "unit": "Wh"}]}]})


def _cobranca_da_sessao(sessao_id: int) -> Cobranca:
    db = SessionLocal()
    try:
        return db.get(Cobranca, db.get(SessaoCarga, sessao_id).cobranca_id)
    finally:
        db.close()


def test_para_quando_entrega_os_kwh_pagos(client):
    pacote_id, tablet = _carregador_kwh(client, "CP-KWH1")
    with client.websocket_connect("/ocpp/CP-KWH1", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        info = client.get("/api/v1/carregador/carregadores/CP-KWH1", headers=tablet).json()
        assert info["modo_cobranca"] == "kwh" and [p["kwh"] for p in info["pacotes"]] == [10]

        sessao_id = _comecar(client, cp, "CP-KWH1", pacote_id, tablet)
        sessao = client.get(f"/api/v1/carregador/sessoes/{sessao_id}", headers=tablet).json()
        assert sessao["kwh_pagos"] == 10 and sessao["minutos_pagos"] == 480  # teto padrao

        _medir(cp, sessao_id, 5000)  # 4 kWh: continua
        sessoes.processar_sessoes_uma_vez()
        assert _sessao(sessao_id).status == "carregando"
        assert client.get(f"/api/v1/carregador/sessoes/{sessao_id}", headers=tablet).json()["kwh_restantes"] == 6

        _medir(cp, sessao_id, 10980)  # 9,98 kWh: dentro da folga, para
        sessoes.processar_sessoes_uma_vez()
        stop = cp.receber_chamada("RemoteStopTransaction")
        cp.responder(stop, {"status": "Accepted"})
        assert _sessao(sessao_id).motivo_fim == "energia_atingida"
        cp.chamar("StopTransaction", {"transactionId": sessao_id, "meterStop": 11000, "timestamp": "2026-11-18T13:00:00Z", "reason": "Remote"})

        sessoes.processar_sessoes_uma_vez()
        cobranca = _cobranca_da_sessao(sessao_id)
        assert cobranca.valor_consumido == 20 and not cobranca.valor_devolvido
        assert _sessao(sessao_id).acerto_em is not None


def test_carro_encheu_antes_devolve_a_sobra(client):
    pacote_id, tablet = _carregador_kwh(client, "CP-KWH2")
    with client.websocket_connect("/ocpp/CP-KWH2", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        sessao_id = _comecar(client, cp, "CP-KWH2", pacote_id, tablet, meter_start=0)
        _medir(cp, sessao_id, 300)
        _medir(cp, sessao_id, 4000)
        # Motorista tirou o cabo com 4 kWh; o JST-901 manda meterStop "0" e
        # isso nao pode zerar o consumo (devolveria tudo).
        cp.chamar("StopTransaction", {"transactionId": str(sessao_id), "meterStop": "0", "timestamp": "2026-11-18T13:00:00Z", "reason": "EVDisconnected"})
        sessoes.processar_sessoes_uma_vez()

    cobranca = _cobranca_da_sessao(sessao_id)
    assert cobranca.valor_consumido == 8.0  # 4 kWh x R$ 2,00
    assert cobranca.valor_devolvido == 12.0
    assert cobranca.estornada_em is None
    tela = client.get(f"/api/v1/carregador/cobrancas/{cobranca.id}", headers=tablet).json()
    assert tela["valor_devolvido"] == 12.0
    recarga = next(s for s in client.get("/api/v1/admin/sessoes?carregador_id=CP-KWH2", headers=ADMIN).json() if s["id"] == sessao_id)
    assert recarga["valor_consumido"] == 8.0 and recarga["valor_devolvido"] == 12.0 and recarga["acerto_erro"] is None
    # Segunda passada do worker nao devolve de novo.
    sessoes.processar_sessoes_uma_vez()
    assert _cobranca_da_sessao(sessao_id).valor_devolvido == 12.0


def test_sem_leitura_do_medidor_nao_devolve_e_avisa(client):
    pacote_id, tablet = _carregador_kwh(client, "CP-KWH3")
    with client.websocket_connect("/ocpp/CP-KWH3", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        r = client.post("/api/v1/carregador/cobrancas", json={"carregador_id": "CP-KWH3", "pacote_id": pacote_id}, headers=tablet)
        sessao_id = client.post(f"/api/v1/carregador/cobrancas/{r.json()['id']}/simular-pagamento", headers=tablet).json()["sessao"]["id"]
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})
        # Sem meterStart e sem MeterValues de energia, so corrente.
        cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "timestamp": "2026-11-18T12:00:00Z"})
        cp.chamar("MeterValues", {"connectorId": 1, "transactionId": sessao_id, "meterValue": [{"timestamp": "2026-11-18T12:10:00Z", "sampledValue": [{"value": "16", "measurand": "Current.Import", "unit": "A"}]}]})
        cp.chamar("StopTransaction", {"transactionId": sessao_id, "timestamp": "2026-11-18T13:00:00Z", "reason": "EVDisconnected"})
        sessoes.processar_sessoes_uma_vez()

    sessao = _sessao(sessao_id)
    assert sessao.acerto_em is not None and "medidor" in sessao.acerto_erro
    assert not _cobranca_da_sessao(sessao_id).valor_devolvido


def test_acerto_desconta_do_primeiro_pagamento_e_depois_do_segundo():
    primeiro = Cobranca(id=1, status="aprovada", valor=20, kwh=10, preco_kwh=2)
    segundo = Cobranca(id=2, status="aprovada", valor=10, kwh=5, preco_kwh=2)
    acerto = cobranca_kwh.calcular_acerto([primeiro, segundo], 12.5)
    assert [(c.id, consumido, sobra) for c, consumido, sobra in acerto] == [(1, 20, 0), (2, 5.0, 5.0)]


def test_maquininha_com_margem_entrega_80_por_cento_e_cobra_pelo_preco(client):
    _carregador_kwh(client, "CP-KWH6")
    r = client.patch("/api/v1/admin/carregadores/CP-KWH6", json={"kwh_percentual_entrega": 80}, headers=ADMIN)
    assert r.json()["kwh_percentual_entrega"] == 80
    with client.websocket_connect("/ocpp/CP-KWH6", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        # R$ 10 a R$ 2,00/kWh: 5 kWh pagos, para em 4 kWh (80%).
        simulado = client.post("/api/v1/admin/carregadores/CP-KWH6/simular-pagamento", json={"valor": 10}, headers=ADMIN).json()
        assert simulado["kwh"] == 4.0
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})
        sessao_id = int(start[3]["idTag"][2:])
        cp.chamar("StartTransaction", {"connectorId": 1, "idTag": start[3]["idTag"], "meterStart": 1000, "timestamp": "2026-11-19T12:00:00Z"})
        _medir(cp, sessao_id, 4980)
        sessoes.processar_sessoes_uma_vez()
        stop = cp.receber_chamada("RemoteStopTransaction")
        cp.responder(stop, {"status": "Accepted"})
        # Passou do ponto: 4,1 kWh. Sai da margem, cobrado a R$ 2,00/kWh.
        cp.chamar("StopTransaction", {"transactionId": sessao_id, "meterStop": 5100, "timestamp": "2026-11-19T13:00:00Z", "reason": "Remote"})
        sessoes.processar_sessoes_uma_vez()

    cobranca = _cobranca_da_sessao(sessao_id)
    assert cobranca.valor_consumido == 8.2
    assert cobranca.valor_devolvido == 1.8

    # App: 10 kWh a R$ 2,00 com 80% = Pix de R$ 25 (R$ 20 de energia + margem).
    info = client.get("/api/v1/publico/carregadores/CP-KWH6").json()
    assert {"kwh": 10, "valor": 25.0} in info["opcoes"] and info["percentual_entrega"] == 80
    # Tablet: pacote de 10 kWh por R$ 20 para em 8 kWh, cobrado a R$ 2,00.
    db = SessionLocal()
    try:
        from app.models.carregador import Carregador, PacoteTempo

        carregador = db.get(Carregador, "CP-KWH6")
        pacote = PacoteTempo(carregador_id="CP-KWH6", nome="10 kWh", minutos=0, kwh=10, valor=20)
        assert cobranca_kwh.kwh_do_pacote(carregador, pacote.kwh) == 8.0
    finally:
        db.close()


def test_devolucao_parcial_manda_o_valor_ao_mercado_pago(client, monkeypatch):
    chamadas = []

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        if "refund" in url:  # so as devolucoes (outros testes podem ter chamadas no fundo)
            chamadas.append((url, body, (headers or {}).get("X-Idempotency-Key")))
        return {}

    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)
    client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": "CP-KWH4", "nome_local": "x"}, headers=ADMIN)
    db = SessionLocal()
    try:
        point = Cobranca(carregador_id="CP-KWH4", minutos=480, kwh=10, valor=20, origem="point", status="aprovada",
                         external_reference="EV:CP-KWH4:a", mp_order_id="ORD1", mp_payment_id="PAY1")
        caixa = Cobranca(carregador_id="CP-KWH4", minutos=480, kwh=10, valor=20, origem="caixa", status="aprovada",
                         external_reference="EV:CP-KWH4:b", mp_payment_id="123")
        db.add_all([point, caixa])
        db.commit()
        assert pagamentos.devolver_parte(db, point, 7.5)
        assert pagamentos.devolver_parte(db, caixa, 7.5)
        assert not pagamentos.devolver_parte(db, caixa, 7.5)  # so uma vez
        assert chamadas == [
            ("https://api.mercadopago.com/v1/orders/ORD1/refund", {"transactions": [{"id": "PAY1", "amount": "7.50"}]}, "devolve-EV:CP-KWH4:a"),
            ("https://api.mercadopago.com/v1/payments/123/refunds", {"amount": 7.5}, "devolve-EV:CP-KWH4:b"),
        ]
        # Estorno pelo painel depois da devolucao: so o que ainda nao voltou.
        assert pagamentos.estornar_cobranca(db, caixa)
        assert chamadas[-1] == ("https://api.mercadopago.com/v1/payments/123/refunds", {"amount": 12.5}, "refund-resto-EV:CP-KWH4:b")
    finally:
        db.close()


def test_app_do_motorista_compra_kwh_no_pix(client):
    _carregador_kwh(client, "CP-KWH5")
    with client.websocket_connect("/ocpp/CP-KWH5", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        info = client.get("/api/v1/publico/carregadores/CP-KWH5").json()
        assert info["modo_cobranca"] == "kwh" and info["preco_kwh"] == 2.0
        assert {"kwh": 10, "valor": 20.0} in info["opcoes"]
        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-KWH5", "minutos": 30, "dispositivo": "celular-kwh-1"})
        assert r.status_code == 422  # carregador por kWh nao vende minutos
        # Sem conta tambem compra kWh (a sobra volta no proprio Pix).
        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-KWH5", "kwh": 15, "dispositivo": "celular-kwh-1"})
        assert r.status_code == 200, r.text
        assert r.json()["kwh"] == 15 and r.json()["valor"] == 30.0 and info["precisa_conta"] is False


def test_previsao_de_termino_pela_potencia():
    from datetime import timedelta

    from app.models.carregador import Carregador, agora

    carregador = Carregador(id_hardware="CP-PREV", medido_potencia_w=7000, medido_em=agora())
    sessao = SessaoCarga(status="carregando", kwh_pagos=10, meter_inicio_wh=0, meter_atual_wh=3000, fim_previsto=agora() + timedelta(hours=8))
    # Faltam 7 kWh a 7 kW: cerca de 1 hora.
    assert 3590 <= cobranca_kwh.previsao_segundos(None, carregador, sessao) <= 3600
    # Nunca passa do tempo maximo.
    sessao.fim_previsto = agora() + timedelta(minutes=30)
    assert cobranca_kwh.previsao_segundos(None, carregador, sessao) <= 1800
