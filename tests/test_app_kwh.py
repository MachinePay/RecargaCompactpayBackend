"""Recarga por kWh pelo app (sem carteira): a sobra volta no mesmo Pix ou
cartao por estorno parcial. Conta no app e' opcional."""
from app.db.session import SessionLocal
from app.models.carregador import Cobranca
from app.services import pagamentos, sessoes
from tests.test_fluxo_carga import ADMIN, CarregadorFake, cliente_teste, client  # noqa: F401 - fixture
from tests.test_kwh import _cobranca_da_sessao, _medir

CELULAR = "celular-app-kwh-1"


def _carregador(client, cp_id: str, **extra) -> None:
    client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": cp_id, "nome_local": "Posto app"}, headers=ADMIN)
    r = client.patch(f"/api/v1/admin/carregadores/{cp_id}", json={"modo_cobranca": "kwh", "preco_kwh": 2.0, **extra}, headers=ADMIN)
    assert r.status_code == 200, r.text


def _carregar(cp, cobranca: dict, kwh_usados: float) -> int:
    sessao_id = cobranca["sessao"]["id"]
    start = cp.receber_chamada("RemoteStartTransaction")
    cp.responder(start, {"status": "Accepted"})
    fim = 1000 + int(kwh_usados * 1000)
    cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": 1000, "timestamp": "2026-11-24T12:00:00Z"})
    _medir(cp, sessao_id, fim)
    cp.chamar("StopTransaction", {"transactionId": sessao_id, "meterStop": fim, "timestamp": "2026-11-24T13:00:00Z", "reason": "EVDisconnected"})
    sessoes.processar_sessoes_uma_vez()
    return sessao_id


def test_pix_do_app_sem_conta_devolve_a_sobra_no_pix(client):
    _carregador(client, "CP-APP1", kwh_percentual_entrega=80)
    with client.websocket_connect("/ocpp/CP-APP1", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        info = client.get("/api/v1/publico/carregadores/CP-APP1").json()
        assert info["precisa_conta"] is False
        # 10 kWh a R$ 2,00 com 80%: energia R$ 20 + reserva R$ 5 = R$ 25.
        previa = client.post("/api/v1/publico/recarga/previa", json={"carregador_id": "CP-APP1", "kwh": 10}).json()
        assert (previa["custo_energia"], previa["reserva"], previa["a_pagar"]) == (20, 5, 25)
        r = client.post("/api/v1/publico/recarga", json={"carregador_id": "CP-APP1", "kwh": 10, "metodo": "pix", "dispositivo": CELULAR, "a_pagar": 25})
        assert r.status_code == 200, r.text
        assert r.json()["carteira"] is False and r.json()["valor"] == 25
        pago = client.post(f"/api/v1/publico/cobrancas/{r.json()['id']}/simular-pagamento", params={"dispositivo": CELULAR}).json()

        # Usou 4 kWh = R$ 8: R$ 17 voltam no proprio Pix.
        sessao_id = _carregar(cp, pago, 4)
    cobranca = _cobranca_da_sessao(sessao_id)
    assert (cobranca.valor_consumido, cobranca.valor_devolvido, cobranca.valor_creditado) == (8.0, 17.0, None)


def test_cartao_do_app_usa_95_e_nao_devolve(client, monkeypatch):
    _carregador(client, "CP-APP2")
    with client.websocket_connect("/ocpp/CP-APP2", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        # Preco mudou entre a previa e o pagamento: nao cobra outro valor.
        r = client.post("/api/v1/publico/recarga", json={"carregador_id": "CP-APP2", "kwh": 10, "metodo": "cartao", "dispositivo": CELULAR, "a_pagar": 13})
        assert r.status_code == 409
        # Cartao: 10 kWh a R$ 2,00 com 95% = R$ 21,05 (sem devolucao).
        previa = client.post("/api/v1/publico/recarga/previa", json={"carregador_id": "CP-APP2", "kwh": 10}).json()
        assert previa["cartao"]["a_pagar"] == 21.05 and previa["cartao"]["devolve_sobra"] is False
        r = client.post("/api/v1/publico/recarga", json={"carregador_id": "CP-APP2", "kwh": 10, "metodo": "cartao", "dispositivo": CELULAR, "a_pagar": 21.05})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "aprovada" and r.json()["valor"] == 21.05 and r.json()["forma"] == "cartao"
        sessao_id = _carregar(cp, r.json(), 7.5)
    cobranca = _cobranca_da_sessao(sessao_id)
    # Usou 7,5 kWh = R$ 15, mas cartao nao tem devolucao.
    assert cobranca.valor_consumido == 15.0 and not cobranca.valor_devolvido


def test_devolucao_do_cartao_do_app_vai_no_payment(client, monkeypatch):
    chamadas = []

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        if "refund" in url:
            chamadas.append((url, body))
        return {}

    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)
    _carregador(client, "CP-APP3")
    db = SessionLocal()
    try:
        cobranca = Cobranca(carregador_id="CP-APP3", minutos=480, kwh=10, valor=20, origem="cartao_app", metodo="credit_card", status="aprovada",
                            external_reference="EV:CP-APP3:c", mp_payment_id="999", dispositivo_id=CELULAR)
        db.add(cobranca)
        db.commit()
        assert pagamentos.devolver_parte(db, cobranca, 5)
        assert chamadas == [("https://api.mercadopago.com/v1/payments/999/refunds", {"amount": 5.0})]
    finally:
        db.close()


def test_maquininha_cartao_converte_95_e_nao_devolve(client):
    _carregador(client, "CP-APP4", kwh_percentual_entrega=80)
    with client.websocket_connect("/ocpp/CP-APP4", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        # Cartao: R$ 10 x 95% / R$ 2,00 = 4,75 kWh (no Pix seriam 4 kWh, 80%).
        simulado = client.post("/api/v1/admin/carregadores/CP-APP4/simular-pagamento", json={"valor": 10, "metodo": "cartao"}, headers=ADMIN).json()
        assert simulado["kwh"] == 4.75
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})
        sessao_id = int(start[3]["idTag"][2:])
        cp.chamar("StartTransaction", {"connectorId": 1, "idTag": start[3]["idTag"], "meterStart": 1000, "timestamp": "2026-11-24T12:00:00Z"})
        _medir(cp, sessao_id, 3000)
        cp.chamar("StopTransaction", {"transactionId": sessao_id, "meterStop": 3000, "timestamp": "2026-11-24T13:00:00Z", "reason": "EVDisconnected"})
        sessoes.processar_sessoes_uma_vez()
    cobranca = _cobranca_da_sessao(sessao_id)
    # Usou 2 kWh = R$ 4, mas cartao na maquininha nao tem devolucao.
    assert cobranca.valor_consumido == 4.0 and not cobranca.valor_devolvido


def test_carregador_ganha_numero_para_o_adesivo(client):
    a = client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": "CP-NUM-A", "nome_local": "Num A"}, headers=ADMIN).json()
    b = client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": "CP-NUM-B", "nome_local": "Num B"}, headers=ADMIN).json()
    assert a["numero"] and b["numero"] == a["numero"] + 1
    # O motorista digita o numero no app.
    achado = client.get(f"/api/v1/publico/carregadores/numero/{b['numero']}").json()
    assert achado["id"] == "CP-NUM-B"
    assert client.get("/api/v1/publico/carregadores/numero/999999").status_code == 404
    # Trocar para um numero ja usado e' recusado.
    assert client.patch("/api/v1/admin/carregadores/CP-NUM-B", json={"numero": a["numero"]}, headers=ADMIN).status_code == 409
    assert client.patch("/api/v1/admin/carregadores/CP-NUM-B", json={"numero": 777777}, headers=ADMIN).json()["numero"] == 777777
