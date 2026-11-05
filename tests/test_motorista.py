"""App do motorista: mapa publico, Pix pelo QR code do carregador e historico."""
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Cobranca
from app.services import pagamentos
from tests.test_fluxo_carga import ADMIN, CarregadorFake, _esperar, cliente_teste, client  # noqa: F401 - fixture

CELULAR = "celular-teste-0001"
OUTRO_CELULAR = "celular-teste-0002"


def _carregador(client, cp_id: str, **extra) -> None:
    r = client.post(
        "/api/v1/admin/carregadores",
        json={"cliente_id": cliente_teste(client), "id_hardware": cp_id, "nome_local": f"Posto {cp_id}"},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    r = client.patch(
        f"/api/v1/admin/carregadores/{cp_id}",
        json={"preco_valor": 10, "preco_minutos": 20, "latitude": -23.5, "longitude": -46.6, **extra},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text


def _do_mapa(client, cp_id: str) -> dict:
    return next(c for c in client.get("/api/v1/publico/carregadores").json() if c["id"] == cp_id)


def test_pix_simulado_libera_recarga_e_entra_no_historico(client):
    _carregador(client, "CP-APP")
    assert _do_mapa(client, "CP-APP")["situacao"] == "offline"

    with client.websocket_connect("/ocpp/CP-APP", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        info = client.get("/api/v1/publico/carregadores/CP-APP").json()
        assert info["situacao"] == "livre" and info["aceita_pix"]
        # R$ 10 = 20 min -> 1 h custa R$ 30.
        assert info["preco_hora"] == 30
        assert {"minutos": 30, "valor": 15.0} in info["opcoes"]

        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-APP", "minutos": 5, "dispositivo": CELULAR})
        assert r.status_code == 422

        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-APP", "minutos": 30, "dispositivo": CELULAR})
        assert r.status_code == 200, r.text
        cobranca = r.json()
        assert cobranca["valor"] == 15 and cobranca["pix_copia_cola"] and cobranca["segundos_para_vencer"] > 0

        # Enquanto um paga, o carregador aparece reservado e outro celular nao gera Pix.
        assert _do_mapa(client, "CP-APP")["situacao"] == "reservado"
        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-APP", "minutos": 30, "dispositivo": OUTRO_CELULAR})
        assert r.status_code == 409

        # O mesmo celular trocando o tempo cancela o Pix anterior.
        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-APP", "minutos": 60, "dispositivo": CELULAR})
        assert r.status_code == 200, r.text
        segunda = r.json()
        primeira = client.get(f"/api/v1/publico/cobrancas/{cobranca['id']}", params={"dispositivo": CELULAR}).json()
        assert primeira["status"] == "cancelada" and primeira["pix_copia_cola"] is None

        # Outro celular nao enxerga a recarga.
        r = client.get(f"/api/v1/publico/cobrancas/{segunda['id']}", params={"dispositivo": OUTRO_CELULAR})
        assert r.status_code == 404

        pago = client.post(f"/api/v1/publico/cobrancas/{segunda['id']}/simular-pagamento", params={"dispositivo": CELULAR}).json()
        assert pago["status"] == "aprovada"
        sessao_id = pago["sessao"]["id"]
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})
        cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": 1000, "timestamp": "2026-11-05T12:00:00Z"})
        cp.chamar("MeterValues", {"connectorId": 1, "transactionId": sessao_id, "meterValue": [{"timestamp": "2026-11-05T12:10:00Z", "sampledValue": [{"value": "3500", "unit": "Wh"}]}]})

        mapa = _do_mapa(client, "CP-APP")
        assert mapa["situacao"] == "ocupado" and 3590 <= mapa["segundos_para_liberar"] <= 3600

        historico = client.get("/api/v1/publico/historico", params={"dispositivo": CELULAR}).json()
        assert historico["totais"]["recargas"] == 1
        assert historico["totais"]["valor"] == 30
        assert historico["totais"]["kwh"] == 2.5
        assert historico["em_andamento"] == [segunda["id"]]
        assert client.get("/api/v1/publico/historico", params={"dispositivo": OUTRO_CELULAR}).json()["totais"]["recargas"] == 0


def test_carregador_sem_preco_nao_aceita_pix(client):
    _carregador(client, "CP-SEMPRECO", preco_valor=None, preco_minutos=None)
    with client.websocket_connect("/ocpp/CP-SEMPRECO", subprotocols=["ocpp1.6"]) as ws:
        CarregadorFake(ws).ligar()
        assert client.get("/api/v1/publico/carregadores/CP-SEMPRECO").json()["aceita_pix"] is False
        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-SEMPRECO", "minutos": 30, "dispositivo": CELULAR})
        assert r.status_code == 422


def test_dispositivo_invalido_e_recusado(client):
    assert client.get("/api/v1/publico/historico", params={"dispositivo": "x"}).status_code == 422


def test_pix_de_verdade_no_mercado_pago(client, monkeypatch):
    """Sem MP_SIMULADO: gera o Pix no MP e libera pelo webhook de payment."""
    _carregador(client, "CP-PIXMP", mp_access_token="TEST-token")
    chamadas = []
    pagamento = {"id": 777, "status": "pending", "transaction_amount": 15.0}

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        chamadas.append((method, url, body))
        if method == "POST" and url.endswith("/v1/payments"):
            assert body["payment_method_id"] == "pix" and body["transaction_amount"] == 15.0
            return {**pagamento, "point_of_interaction": {"transaction_data": {"qr_code": "000201PIXREAL"}}}
        if method == "GET" and url.endswith("/v1/payments/777"):
            return pagamento
        if url.endswith("/v1/payments/777/refunds"):
            return {"id": 1}
        raise AssertionError(url)

    monkeypatch.setattr(settings, "MP_SIMULADO", False)
    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)
    with client.websocket_connect("/ocpp/CP-PIXMP", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        r = client.post("/api/v1/publico/pix", json={"carregador_id": "CP-PIXMP", "minutos": 30, "dispositivo": CELULAR})
        assert r.status_code == 200, r.text
        cobranca = r.json()
        assert cobranca["pix_copia_cola"] == "000201PIXREAL" and not cobranca["simulado"]

        # Webhook com o Pix ainda pendente nao libera nada.
        client.post("/api/v1/callback-mercado-pago", json={"type": "payment", "data": {"id": "777"}})
        assert client.get(f"/api/v1/publico/cobrancas/{cobranca['id']}", params={"dispositivo": CELULAR}).json()["status"] == "aguardando_pagamento"

        pagamento["status"] = "approved"
        client.post("/api/v1/callback-mercado-pago", json={"type": "payment", "data": {"id": "777"}})
        cp.receber_chamada("RemoteStartTransaction")
        _esperar(
            lambda: client.get(f"/api/v1/publico/cobrancas/{cobranca['id']}", params={"dispositivo": CELULAR}).json()["status"] == "aprovada"
        )

    db = SessionLocal()
    try:
        venda = db.get(Cobranca, cobranca["id"])
        assert venda.origem == "pix" and venda.metodo == "pix" and venda.mp_payment_id == "777"
        # Estorno do Pix vai pelo payment.
        assert pagamentos.estornar_cobranca(db, venda)
    finally:
        db.close()
    assert ("POST", "https://api.mercadopago.com/v1/payments/777/refunds", None) in chamadas
