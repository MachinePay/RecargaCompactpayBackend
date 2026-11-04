"""SumUp igual a CompactPay: chave do cliente, maquininha pareada e poller."""
from app.db.session import SessionLocal
from app.models.carregador import Cobranca, SessaoCarga
from app.services import sumup
from tests.test_fluxo_carga import ADMIN, CarregadorFake, _esperar, client  # noqa: F401 - fixture


def test_cliente_sumup_carregador_pareado_e_poller_libera_pela_maquininha(client, monkeypatch):
    estornos = []
    historico = [
        {"id": "TX-CARTAO", "status": "SUCCESSFUL", "amount": 10, "payment_type": "POS"},
        {"id": "TX-OUTRA", "status": "SUCCESSFUL", "amount": 50},
        {"id": "TX-FALHOU", "status": "FAILED", "amount": 10},
    ]
    seriais = {"TX-CARTAO": "SERIAL-1", "TX-PIX": None, "TX-OUTRA": "SERIAL-DE-OUTRO-NEGOCIO"}

    def su_falso(method, url, api_key, body=None):
        if url.endswith("/v0.1/me"):
            if api_key == "sup_sk_ruim":
                raise sumup.HTTPException(status_code=502, detail="SumUp retornou erro 401: invalid")
            return {"merchant_profile": {"merchant_code": "MC1"}}
        if url.endswith("/merchants/MC1/readers") and method == "GET":
            return {"items": [{"id": "rdr_1", "name": "Solo 1", "device": {"identifier": "SERIAL-1"}}]}
        if url.endswith("/merchants/MC1/readers") and method == "POST":
            return {"id": "rdr_2", "name": body.get("name"), "device": {"identifier": "SERIAL-2"}}
        if "/transactions/history" in url:
            return {"items": historico}
        if "/receipts/" in url:
            tx = url.split("/receipts/")[1].split("?")[0]
            return {"transaction_data": {"card_reader": {"code": seriais.get(tx)}}}
        if "/me/refund/" in url:
            estornos.append(url.rsplit("/", 1)[-1])
            return {}
        raise AssertionError(url)

    monkeypatch.setattr(sumup, "su_request", su_falso)
    assert client.post("/api/v1/admin/clientes", json={"nome_empresa": "Y", "cliente_sumup": True, "sumup_api_key": "sup_sk_ruim"}, headers=ADMIN).status_code == 422
    cliente = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Posto SumUp", "cliente_sumup": True, "sumup_api_key": "sup_sk_bom"}, headers=ADMIN).json()
    assert cliente["bancos"] == ["sumup"] and cliente["sumup_merchant_code"] == "MC1" and "sumup_api_key" not in cliente

    leitores = client.get(f"/api/v1/admin/clientes/{cliente['id']}/sumup/readers", headers=ADMIN).json()
    assert leitores[0]["serial"] == "SERIAL-1"
    assert client.post(f"/api/v1/admin/clientes/{cliente['id']}/sumup/readers", json={"pairing_code": "ABC123", "nome": "Solo 2"}, headers=ADMIN).json()["id"] == "rdr_2"

    base = {"id_hardware": "CP-SUMUP", "nome_local": "Posto", "cliente_id": cliente["id"]}
    assert client.post("/api/v1/admin/carregadores", json={**base, "banco_pagamento": "mercado_pago"}, headers=ADMIN).status_code == 422
    assert client.post("/api/v1/admin/carregadores", json={**base, "banco_pagamento": "sumup"}, headers=ADMIN).status_code == 422
    r = client.post("/api/v1/admin/carregadores", json={**base, "banco_pagamento": "sumup", "sumup_reader_id": "rdr_1", "sumup_usuario": "operador1"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["banco_pagamento"] == "sumup"
    client.patch("/api/v1/admin/carregadores/CP-SUMUP", json={"preco_valor": 10, "preco_minutos": 20}, headers=ADMIN)

    with client.websocket_connect("/CP-SUMUP") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        sumup.processar_uma_vez()
        start = cp.receber_chamada("RemoteStartTransaction")  # TX-CARTAO: R$ 10 = 20 min
        cp.responder(start, {"status": "Accepted"})
        cp.chamar("StartTransaction", {"idTag": start[3]["idTag"], "connectorId": 1, "timestamp": "2026-11-04T00:00:00Z", "meterStart": 0})
        sessao_id = int(start[3]["idTag"][2:])
        # Proxima leitura: o Pix do operador soma 10 min; nada repete.
        historico.append({"id": "TX-PIX", "status": "SUCCESSFUL", "amount": 5, "payment_type": "PIX", "user": "Operador1"})
        sumup.processar_uma_vez()

    db = SessionLocal()
    try:
        cobrancas = {c.mp_payment_id: c for c in db.query(Cobranca).filter(Cobranca.origem == "sumup").all()}
        assert set(cobrancas) == {"TX-CARTAO", "TX-PIX"}
        assert db.get(SessaoCarga, sessao_id).minutos_pagos == 30
    finally:
        db.close()
    assert estornos == []
