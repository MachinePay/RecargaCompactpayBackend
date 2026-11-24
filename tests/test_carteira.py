"""Carteira do motorista: recarga por kWh pelo app, sobra volta para a carteira."""
from app.db.session import SessionLocal
from app.models.carregador import Cobranca, Motorista, MovimentoCarteira
from app.services import carteira, pagamentos, sessoes
from tests.test_fluxo_carga import ADMIN, CarregadorFake, _sessao, cliente_teste, client  # noqa: F401 - fixture
from tests.test_kwh import _cobranca_da_sessao, _medir

CELULAR = "celular-carteira-1"


def _carregador(client, cp_id: str, **extra) -> None:
    client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": cp_id, "nome_local": "Posto carteira"}, headers=ADMIN)
    r = client.patch(f"/api/v1/admin/carregadores/{cp_id}", json={"modo_cobranca": "kwh", "preco_kwh": 2.0, **extra}, headers=ADMIN)
    assert r.status_code == 200, r.text


def _conta(client, telefone: str) -> dict:
    r = client.post(
        "/api/v1/publico/conta",
        json={"nome": "Motorista Teste", "telefone": telefone, "email": f"m{telefone}@exemplo.com", "senha": "segredo123", "dispositivo": CELULAR},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _dar_saldo(telefone: str, valor: float) -> None:
    db = SessionLocal()
    try:
        motorista = db.query(Motorista).filter(Motorista.telefone == telefone).one()
        carteira.movimentar(db, motorista.id, valor, "ajuste", "teste")
    finally:
        db.close()


def _saldo(client, login) -> float:
    return client.get("/api/v1/publico/carteira", headers=login).json()["saldo"]


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


def _recarga(client, login, cp_id: str, kwh: float, metodo: str, a_pagar=None):
    corpo = {"carregador_id": cp_id, "kwh": kwh, "metodo": metodo, "dispositivo": CELULAR}
    if a_pagar is not None:
        corpo["a_pagar"] = a_pagar
    return client.post("/api/v1/publico/recarga", json=corpo, headers=login)


def test_pix_pelo_app_sobra_vai_para_a_carteira_e_paga_a_proxima(client):
    _carregador(client, "CP-CART1", kwh_percentual_entrega=80)
    login = _conta(client, "11911110001")
    with client.websocket_connect("/ocpp/CP-CART1", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        # Sem conta nao paga.
        assert _recarga(client, {}, "CP-CART1", 10, "pix").status_code == 401
        # 10 kWh a R$ 2,00 com 80%: energia R$ 20 + reserva R$ 5 = R$ 25.
        previa = client.post("/api/v1/publico/recarga/previa", json={"carregador_id": "CP-CART1", "kwh": 10}, headers=login).json()
        assert (previa["custo_energia"], previa["reserva"], previa["total"], previa["a_pagar"]) == (20, 5, 25, 25)
        r = _recarga(client, login, "CP-CART1", 10, "pix", 25)
        assert r.status_code == 200, r.text
        pago = client.post(f"/api/v1/publico/cobrancas/{r.json()['id']}/simular-pagamento", params={"dispositivo": CELULAR}).json()
        assert pago["carteira"] is True and pago["total"] == 25

        # Usou 4 kWh = R$ 8: R$ 17 voltam para a carteira (nada pelo banco).
        sessao_id = _carregar(cp, pago, 4)
        cobranca = _cobranca_da_sessao(sessao_id)
        assert (cobranca.valor_consumido, cobranca.valor_creditado, cobranca.valor_devolvido) == (8.0, 17.0, None)
        assert _saldo(client, login) == 17.0

        # Proxima: 5 kWh = R$ 12,50 com a reserva; o saldo paga tudo.
        previa = client.post("/api/v1/publico/recarga/previa", json={"carregador_id": "CP-CART1", "kwh": 5}, headers=login).json()
        assert (previa["saldo_usado"], previa["a_pagar"]) == (12.5, 0)
        r = _recarga(client, login, "CP-CART1", 5, "saldo", 0)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "aprovada" and r.json()["forma"] == "saldo"
        assert _saldo(client, login) == 4.5
        sessao_id = r.json()["sessao"]["id"]
        cp.receber_chamada("RemoteStartTransaction")  # o carro nunca conectou
        db = SessionLocal()
        try:
            sessoes.falhar_sessao(db, db.get(type(_sessao(sessao_id)), sessao_id), "carro_nao_conectado")
        finally:
            db.close()
        # A recarga nao aconteceu: tudo volta para a carteira.
        assert _saldo(client, login) == 17.0

    extrato = client.get("/api/v1/publico/carteira", headers=login).json()
    assert [m["tipo"] for m in extrato["itens"]] == ["devolucao", "reserva", "sobra"]


def test_saldo_mais_cartao_completa_a_diferenca(client):
    _carregador(client, "CP-CART2")
    login = _conta(client, "11911110002")
    _dar_saldo("11911110002", 6)
    with client.websocket_connect("/ocpp/CP-CART2", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        previa = client.post("/api/v1/publico/recarga/previa", json={"carregador_id": "CP-CART2", "kwh": 10}, headers=login).json()
        assert (previa["total"], previa["saldo_usado"], previa["a_pagar"]) == (20, 6, 14)
        # O valor visto nao bate (saldo mudou): nao cobra outro valor.
        assert _recarga(client, login, "CP-CART2", 10, "cartao", 13).status_code == 409
        r = _recarga(client, login, "CP-CART2", 10, "cartao", 14)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "aprovada" and r.json()["valor"] == 14 and r.json()["valor_carteira"] == 6
        assert _saldo(client, login) == 0
        # Usou os 10 kWh = R$ 20: nada sobra.
        sessao_id = _carregar(cp, r.json(), 10)
        assert _cobranca_da_sessao(sessao_id).valor_consumido == 20
        assert _saldo(client, login) == 0


def test_pix_cancelado_devolve_o_saldo_separado_uma_vez(client):
    _carregador(client, "CP-CART3")
    login = _conta(client, "11911110003")
    _dar_saldo("11911110003", 5)
    with client.websocket_connect("/ocpp/CP-CART3", subprotocols=["ocpp1.6"]) as ws:
        CarregadorFake(ws).ligar()
        r = _recarga(client, login, "CP-CART3", 10, "pix")
        assert r.status_code == 200 and r.json()["valor"] == 15 and _saldo(client, login) == 0
        client.post(f"/api/v1/publico/cobrancas/{r.json()['id']}/cancelar", params={"dispositivo": CELULAR})
        assert _saldo(client, login) == 5
        db = SessionLocal()
        try:
            pagamentos._devolver_reserva(db, db.get(Cobranca, r.json()["id"]))
            devolucoes = db.query(MovimentoCarteira).filter(MovimentoCarteira.cobranca_id == r.json()["id"], MovimentoCarteira.tipo == "devolucao").count()
            assert devolucoes == 1
        finally:
            db.close()


def test_maquininha_cartao_converte_95_e_nao_devolve(client):
    _carregador(client, "CP-CART4", kwh_percentual_entrega=80)
    with client.websocket_connect("/ocpp/CP-CART4", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        # Cartao: R$ 10 x 95% / R$ 2,00 = 4,75 kWh (no Pix seriam 4 kWh, 80%).
        simulado = client.post("/api/v1/admin/carregadores/CP-CART4/simular-pagamento", json={"valor": 10, "metodo": "cartao"}, headers=ADMIN).json()
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
