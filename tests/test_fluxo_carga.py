import base64
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.db.session import SessionLocal
from app.main import app
from app.models.carregador import Cobranca, SessaoCarga, agora
from app.services import pagamentos, sessoes

ADMIN = {"X-Admin-Key": "admin-teste"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class CarregadorFake:
    """Faz o papel do wallbox do outro lado do WebSocket."""

    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    def chamar(self, acao: str, payload: dict) -> dict:
        self.n += 1
        msg_id = f"m{self.n}"
        self.ws.send_json([2, msg_id, acao, payload])
        resposta = self.ws.receive_json()
        assert resposta[0] == 3 and resposta[1] == msg_id, resposta
        return resposta[2]

    def receber_chamada(self, acao_esperada: str) -> list:
        msg = self.ws.receive_json()
        assert msg[0] == 2 and msg[2] == acao_esperada, msg
        return msg

    def responder(self, msg: list, payload: dict) -> None:
        self.ws.send_json([3, msg[1], payload])

    def ligar(self, status: str = "Available") -> None:
        assert self.chamar("BootNotification", {"chargePointVendor": "Fake", "chargePointModel": "WB7"})["status"] == "Accepted"
        self.chamar("StatusNotification", {"connectorId": 1, "errorCode": "NoError", "status": status})


def _setup(client, cp_id: str, **extra) -> tuple[int, dict]:
    r = client.post("/api/v1/admin/carregadores", json={"id_hardware": cp_id, "nome_local": "Posto teste", **extra}, headers=ADMIN)
    assert r.status_code == 200, r.text
    pacote = client.post(
        f"/api/v1/admin/carregadores/{cp_id}/pacotes", json={"nome": "30 min", "minutos": 30, "valor": 10}, headers=ADMIN
    ).json()
    chave = client.post("/api/v1/admin/terminais", json={"nome": f"tablet {cp_id}"}, headers=ADMIN).json()["chave"]
    return pacote["id"], {"X-Terminal-Key": chave}


def _esperar(condicao, timeout: float = 3.0):
    limite = time.time() + timeout
    while time.time() < limite:
        valor = condicao()
        if valor:
            return valor
        time.sleep(0.05)
    raise AssertionError("condicao nao aconteceu a tempo")


def _sessao(sessao_id: int) -> SessaoCarga:
    db = SessionLocal()
    try:
        return db.get(SessaoCarga, sessao_id)
    finally:
        db.close()


def _alterar_sessao(sessao_id: int, **campos) -> None:
    db = SessionLocal()
    try:
        db.query(SessaoCarga).filter(SessaoCarga.id == sessao_id).update(campos)
        db.commit()
    finally:
        db.close()


def _pagar(client, cp_id: str, pacote_id: int, headers: dict) -> dict:
    r = client.post("/api/v1/carregador/cobrancas", json={"carregador_id": cp_id, "pacote_id": pacote_id}, headers=headers)
    assert r.status_code == 200, r.text
    cobranca = r.json()
    assert cobranca["status"] == "aguardando_pagamento"
    r = client.post(f"/api/v1/carregador/cobrancas/{cobranca['id']}/simular-pagamento", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_pagamento_liga_carregador_e_desliga_quando_o_tempo_acaba(client):
    pacote_id, tablet = _setup(client, "CP-FLUXO")
    with client.websocket_connect("/ocpp/CP-FLUXO", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        info = client.get("/api/v1/carregador/carregadores/CP-FLUXO", headers=tablet).json()
        assert info["online"] and info["disponivel"] and info["pacotes"][0]["minutos"] == 30

        cobranca = _pagar(client, "CP-FLUXO", pacote_id, tablet)
        assert cobranca["status"] == "aprovada"
        sessao_id = cobranca["sessao"]["id"]

        start = cp.receber_chamada("RemoteStartTransaction")
        assert start[3] == {"connectorId": 1, "idTag": f"EV{sessao_id}"}
        cp.responder(start, {"status": "Accepted"})
        _esperar(lambda: _sessao(sessao_id).status == "aguardando_carro")

        conf = cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": 1000, "timestamp": "2026-10-27T12:00:00Z"})
        assert conf == {"transactionId": sessao_id, "idTagInfo": {"status": "Accepted"}}
        sessao = client.get(f"/api/v1/carregador/sessoes/{sessao_id}", headers=tablet).json()
        assert sessao["status"] == "carregando"
        assert 1790 <= sessao["segundos_restantes"] <= 1800

        # Tablet nao vende enquanto carrega.
        r = client.post("/api/v1/carregador/cobrancas", json={"carregador_id": "CP-FLUXO", "pacote_id": pacote_id}, headers=tablet)
        assert r.status_code == 409

        cp.chamar("MeterValues", {"connectorId": 1, "transactionId": sessao_id, "meterValue": [{"timestamp": "2026-10-27T12:10:00Z", "sampledValue": [{"value": "2.0", "unit": "kWh"}]}]})
        assert client.get(f"/api/v1/carregador/sessoes/{sessao_id}", headers=tablet).json()["energia_kwh"] == 1.0

        _alterar_sessao(sessao_id, fim_previsto=agora() - timedelta(seconds=1))
        sessoes.processar_sessoes_uma_vez()
        stop = cp.receber_chamada("RemoteStopTransaction")
        assert stop[3] == {"transactionId": sessao_id}
        cp.responder(stop, {"status": "Accepted"})
        assert _sessao(sessao_id).status == "encerrando"

        cp.chamar("StopTransaction", {"transactionId": sessao_id, "meterStop": 3500, "timestamp": "2026-10-27T12:30:00Z", "reason": "Remote"})
        final = client.get(f"/api/v1/carregador/sessoes/{sessao_id}", headers=tablet).json()
        assert final["status"] == "finalizada"
        assert final["motivo_fim"] == "tempo_esgotado"
        assert final["energia_kwh"] == 2.5


def test_carro_nao_conectado_a_tempo_falha_e_estorna(client):
    pacote_id, tablet = _setup(client, "CP-SEMCARRO")
    with client.websocket_connect("/ocpp/CP-SEMCARRO") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        cobranca = _pagar(client, "CP-SEMCARRO", pacote_id, tablet)
        sessao_id = cobranca["sessao"]["id"]
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Accepted"})
        _esperar(lambda: _sessao(sessao_id).status == "aguardando_carro")

        _alterar_sessao(sessao_id, created_at=agora() - timedelta(minutes=10))
        sessoes.processar_sessoes_uma_vez()

        r = client.get(f"/api/v1/carregador/cobrancas/{cobranca['id']}", headers=tablet).json()
        assert r["sessao"]["status"] == "falhou"
        assert r["sessao"]["motivo_fim"] == "carro_nao_conectado"
        assert r["estornada"] is True

        # StartTransaction atrasado depois do estorno nao pode carregar.
        conf = cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": 0, "timestamp": "2026-10-27T12:00:00Z"})
        assert conf["idTagInfo"]["status"] == "Invalid"


def test_remote_start_recusado_falha_e_estorna(client):
    pacote_id, tablet = _setup(client, "CP-RECUSA")
    with client.websocket_connect("/ocpp/CP-RECUSA") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        cobranca = _pagar(client, "CP-RECUSA", pacote_id, tablet)
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Rejected"})
        sessao_id = cobranca["sessao"]["id"]
        _esperar(lambda: _sessao(sessao_id).status == "falhou")
        assert client.get(f"/api/v1/carregador/cobrancas/{cobranca['id']}", headers=tablet).json()["estornada"] is True


def test_pagamento_repetido_libera_uma_sessao_so(client):
    pacote_id, tablet = _setup(client, "CP-DUP")
    with client.websocket_connect("/ocpp/CP-DUP") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        cobranca = _pagar(client, "CP-DUP", pacote_id, tablet)
        client.post(f"/api/v1/carregador/cobrancas/{cobranca['id']}/simular-pagamento", headers=tablet)
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Accepted"})
    db = SessionLocal()
    try:
        assert db.query(SessaoCarga).filter(SessaoCarga.cobranca_id == cobranca["id"]).count() == 1
    finally:
        db.close()


def test_carregador_offline_nao_vende(client):
    pacote_id, tablet = _setup(client, "CP-OFF")
    r = client.post("/api/v1/carregador/cobrancas", json={"carregador_id": "CP-OFF", "pacote_id": pacote_id}, headers=tablet)
    assert r.status_code == 409
    assert r.json()["detail"] == "Carregador offline"


def test_stop_e_reenviado_quando_carregador_volta_da_queda(client):
    pacote_id, tablet = _setup(client, "CP-QUEDA")
    with client.websocket_connect("/ocpp/CP-QUEDA") as ws:
        cp = CarregadorFake(ws)
        cp.ligar("Preparing")
        sessao_id = _pagar(client, "CP-QUEDA", pacote_id, tablet)["sessao"]["id"]
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Accepted"})
        cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": 0, "timestamp": "2026-10-27T12:00:00Z"})

    # Caiu a internet do carregador e o tempo acabou nesse meio tempo.
    _esperar(lambda: not sessoes.ocpp_server.esta_conectado("CP-QUEDA"))
    _alterar_sessao(sessao_id, fim_previsto=agora() - timedelta(seconds=1))
    sessoes.processar_sessoes_uma_vez()
    assert _sessao(sessao_id).status == "encerrando"

    with client.websocket_connect("/ocpp/CP-QUEDA") as ws:
        cp = CarregadorFake(ws)
        cp.ligar("Charging")
        _alterar_sessao(sessao_id, ultimo_stop_em=agora() - timedelta(minutes=1))
        sessoes.processar_sessoes_uma_vez()
        stop = cp.receber_chamada("RemoteStopTransaction")
        cp.responder(stop, {"status": "Accepted"})
        cp.chamar("StopTransaction", {"transactionId": sessao_id, "meterStop": 100, "timestamp": "2026-10-27T12:31:00Z", "reason": "Remote"})
    assert _sessao(sessao_id).status == "finalizada"


def test_tag_mestre_carrega_sem_pagamento_e_tag_desconhecida_e_recusada(client):
    _setup(client, "CP-RFID")
    with client.websocket_connect("/ocpp/CP-RFID") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        assert cp.chamar("Authorize", {"idTag": "QUALQUER"})["idTagInfo"]["status"] == "Invalid"
        assert cp.chamar("Authorize", {"idTag": "RFID-MESTRE"})["idTagInfo"]["status"] == "Accepted"
        conf = cp.chamar("StartTransaction", {"connectorId": 1, "idTag": "RFID-MESTRE", "meterStart": 0, "timestamp": "2026-10-27T12:00:00Z"})
        assert conf["idTagInfo"]["status"] == "Accepted"
        sessao = _sessao(conf["transactionId"])
        assert sessao.status == "carregando" and sessao.fim_previsto is None


def test_carregador_nao_cadastrado_ou_com_senha_errada_e_recusado(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ocpp/NAO-EXISTE") as ws:
            ws.receive_json()

    r = client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-SENHA", "nome_local": "x", "gerar_senha_ocpp": True}, headers=ADMIN)
    senha = r.json()["senha_ocpp"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ocpp/CP-SENHA") as ws:
            ws.receive_json()
    auth = "Basic " + base64.b64encode(f"CP-SENHA:{senha}".encode()).decode()
    with client.websocket_connect("/ocpp/v16/CP-SENHA", headers={"Authorization": auth}) as ws:
        assert CarregadorFake(ws).chamar("Heartbeat", {})["currentTime"]


def test_order_paga_na_point_libera_carga_so_com_valor_certo(client, monkeypatch):
    _setup(client, "CP-POINT")
    db = SessionLocal()
    try:
        certa = Cobranca(carregador_id="CP-POINT", minutos=30, valor=10, origem="point", external_reference="EV:CP-POINT:a", mp_order_id="ORD-OK")
        errada = Cobranca(carregador_id="CP-POINT", minutos=30, valor=10, origem="point", external_reference="EV:CP-POINT:b", mp_order_id="ORD-ERRADA")
        db.add_all([certa, errada])
        db.commit()
        certa_id, errada_id = certa.id, errada.id
    finally:
        db.close()

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        pago = "1.00" if "ORD-ERRADA" in url else "10.00"
        return {"id": url.rsplit("/", 1)[-1], "status": "processed", "status_detail": "accredited", "total_paid_amount": pago,
                "transactions": {"payments": [{"id": "PAY1", "paid_amount": pago, "payment_method": {"type": "credit_card"}}]}}

    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)
    monkeypatch.setattr(pagamentos.settings, "MP_ACCESS_TOKEN", "TEST-token")
    with client.websocket_connect("/ocpp/CP-POINT") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        r = client.post("/api/v1/callback-mercado-pago", json={"type": "order", "data": {"id": "ORD-ERRADA"}})
        assert r.status_code == 200
        r = client.post("/api/v1/callback-mercado-pago", json={"type": "order", "data": {"id": "ORD-OK"}})
        assert r.status_code == 200
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})

    db = SessionLocal()
    try:
        assert db.get(Cobranca, certa_id).status == "aprovada"
        assert db.get(Cobranca, certa_id).metodo == "credit_card"
        assert db.get(Cobranca, errada_id).status == "aguardando_pagamento"
        assert db.query(SessaoCarga).filter(SessaoCarga.cobranca_id == errada_id).count() == 0
    finally:
        db.close()


def test_painel_lista_carregador_pacotes_terminais_sessoes_e_resumo(client):
    pacote_id, tablet = _setup(client, "CP-PAINEL")
    with client.websocket_connect("/ocpp/CP-PAINEL") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        sessao_id = _pagar(client, "CP-PAINEL", pacote_id, tablet)["sessao"]["id"]
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Accepted"})
        cp.chamar("StartTransaction", {"connectorId": 1, "idTag": f"EV{sessao_id}", "meterStart": 0, "timestamp": "2026-10-27T12:00:00Z"})

        detalhe = client.get("/api/v1/admin/carregadores/CP-PAINEL", headers=ADMIN).json()
        assert detalhe["online"] and detalhe["sessao_ativa"]["id"] == sessao_id
        lista = client.get("/api/v1/admin/carregadores", headers=ADMIN).json()
        assert any(c["id_hardware"] == "CP-PAINEL" and c["sessao_ativa"] for c in lista)
        assert client.get("/api/v1/admin/carregadores/CP-PAINEL/pacotes", headers=ADMIN).json()[0]["id"] == pacote_id

        sessoes_lista = client.get("/api/v1/admin/sessoes?carregador_id=CP-PAINEL", headers=ADMIN).json()
        assert sessoes_lista[0]["valor"] == 10 and sessoes_lista[0]["origem"] == "simulado"

        resumo = client.get("/api/v1/admin/resumo", headers=ADMIN).json()
        assert resumo["carregadores_online"] >= 1 and resumo["sessoes_ativas"] >= 1
        assert resumo["faturamento_hoje"] >= 10

    terminais = client.get("/api/v1/admin/terminais", headers=ADMIN).json()
    assert terminais and "chave" not in terminais[0]
    assert client.get("/api/v1/admin/resumo", headers={"X-Admin-Key": "errada"}).status_code == 401


def test_diagnostico_mostra_tentativa_recusada_conexao_e_mensagens(client):
    client.delete("/api/v1/admin/diagnostico/eventos", headers=ADMIN)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ID-ERRADO") as ws:
            ws.receive_json()
    servidor = client.get("/api/v1/admin/diagnostico/servidor", headers=ADMIN).json()
    assert [i["id"] for i in servidor["ids_nao_cadastrados"]] == ["ID-ERRADO"]

    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-DIAG", "nome_local": "x"}, headers=ADMIN)
    # Sem /ocpp no caminho, como o app do JST-901 pode montar.
    with client.websocket_connect("/CP-DIAG") as ws:
        CarregadorFake(ws).ligar()
        assert "CP-DIAG" in client.get("/api/v1/admin/diagnostico/servidor", headers=ADMIN).json()["conectados"]

    eventos = client.get("/api/v1/admin/diagnostico/eventos?carregador_id=CP-DIAG", headers=ADMIN).json()
    tipos = [(e["tipo"], e["acao"]) for e in eventos]
    assert ("recusada", "nao_cadastrado") in tipos  # tentativa de ID errado aparece em qualquer filtro
    assert ("aceita", None) in tipos
    assert ("recebida", "BootNotification") in tipos
    assert ("resposta_enviada", "BootNotification") in tipos
    assert tipos[-1] == ("desconectou", None)
    ultimo = eventos[-1]["seq"]
    assert client.get(f"/api/v1/admin/diagnostico/eventos?desde={ultimo}", headers=ADMIN).json() == []

    client.get("/qualquer-coisa")
    assert client.get(f"/api/v1/admin/diagnostico/eventos?desde={ultimo}", headers=ADMIN).json()[0]["tipo"] == "http"
