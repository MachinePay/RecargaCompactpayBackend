"""Venda direto na maquininha vinculada ao caixa do carregador (igual CompactPay)."""
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, inspect
from sqlalchemy.orm import declarative_base

from app.db.migracoes import adicionar_colunas_faltando
from app.db.session import SessionLocal
from app.models.carregador import Cobranca, SessaoCarga
from app.services import pagamentos
from tests.test_fluxo_carga import ADMIN, cliente_teste, CarregadorFake, _esperar, client  # noqa: F401 - fixture


def _carregador_com_caixa(client, cp_id: str, pos_id: str) -> None:
    client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": cp_id, "nome_local": "Garagem"}, headers=ADMIN)
    r = client.patch(
        f"/api/v1/admin/carregadores/{cp_id}",
        json={"preco_valor": 10, "preco_minutos": 20, "mp_access_token": "TEST-token"},
        headers=ADMIN,
    )
    assert r.json()["preco_minutos"] == 20

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        if url.endswith("/users/me"):
            return {"id": 123}
        if "/stores/search" in url or url.startswith("https://api.mercadopago.com/pos?"):
            return {"results": []}
        if url.endswith("/stores"):
            return {"id": f"STORE-{pos_id}", "external_id": body["external_id"]}
        if url.endswith("/pos"):
            return {"id": pos_id, "external_id": body["external_id"]}
        raise AssertionError(url)

    original = pagamentos.mp_request
    pagamentos.mp_request = mp_falso
    try:
        r = client.post(f"/api/v1/admin/carregadores/{cp_id}/mercado-pago/caixa", headers=ADMIN)
    finally:
        pagamentos.mp_request = original
    assert r.status_code == 200, r.text
    assert r.json()["mp_pos_id"] == pos_id


def _pagamentos_falsos(monkeypatch, pagos: dict):
    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        if "/v1/payments/" in url and method == "GET":
            return pagos[url.rsplit("/", 1)[-1]]
        if url.endswith("/refunds"):
            pagos.setdefault("_estornos", []).append(url)
            return {"id": 1}
        raise AssertionError(url)

    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)


def test_pagamento_na_maquininha_do_caixa_libera_tempo_proporcional_e_soma(client, monkeypatch):
    _carregador_com_caixa(client, "CP-CAIXA", "POS-1")
    pagos = {
        "P1": {"id": "P1", "status": "approved", "transaction_amount": 5, "payment_type_id": "debit_card", "pos_id": "POS-1", "store_id": "STORE-POS-1"},
        "P2": {"id": "P2", "status": "approved", "transaction_amount": 10, "payment_type_id": "pix", "pos_id": "POS-1"},
        "OUTRO": {"id": "OUTRO", "status": "approved", "transaction_amount": 10, "pos_id": "POS-DE-OUTRA-LOJA"},
    }
    _pagamentos_falsos(monkeypatch, pagos)

    with client.websocket_connect("/CP-CAIXA") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        # R$ 5 com a regra R$ 10 = 20 min -> 10 min. Webhook repetido nao libera duas vezes.
        client.post("/api/v1/callback-mercado-pago", json={"type": "payment", "data": {"id": "P1"}})
        client.post("/api/v1/callback-mercado-pago", json={"action": "payment.updated", "data": {"id": "P1"}})
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})
        sessao_id = int(start[3]["idTag"][2:])
        cp.chamar("StartTransaction", {"idTag": start[3]["idTag"], "connectorId": 1, "timestamp": "2026-11-04T00:00:00Z", "meterStart": 0})
        assert _sessao_db(sessao_id).minutos_pagos == 10

        # Pagou de novo carregando: soma 20 min. Pagamento de outro caixa: ignora.
        client.post("/api/v1/callback-mercado-pago?topic=payment&id=P2")
        client.post("/api/v1/callback-mercado-pago", json={"type": "payment", "data": {"id": "OUTRO"}})
        _esperar(lambda: _sessao_db(sessao_id).minutos_pagos == 30)

    db = SessionLocal()
    try:
        assert db.query(Cobranca).filter(Cobranca.carregador_id == "CP-CAIXA", Cobranca.origem == "caixa").count() == 2
        assert db.query(Cobranca).filter(Cobranca.mp_payment_id == "OUTRO").count() == 0
    finally:
        db.close()


def test_pagamento_com_recarga_comecando_e_estornado(client, monkeypatch):
    _carregador_com_caixa(client, "CP-CAIXA2", "POS-2")
    pagos = {
        "A": {"id": "A", "status": "approved", "transaction_amount": 10, "pos_id": "POS-2"},
        "B": {"id": "B", "status": "approved", "transaction_amount": 10, "pos_id": "POS-2"},
    }
    _pagamentos_falsos(monkeypatch, pagos)
    with client.websocket_connect("/CP-CAIXA2") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        client.post("/api/v1/callback-mercado-pago", json={"type": "payment", "data": {"id": "A"}})
        cp.receber_chamada("RemoteStartTransaction")  # sem responder: recarga ainda comecando
        client.post("/api/v1/callback-mercado-pago", json={"type": "payment", "data": {"id": "B"}})
        _esperar(lambda: pagos.get("_estornos") == ["https://api.mercadopago.com/v1/payments/B/refunds"])


def test_simular_pagamento_pelo_painel_usa_a_regra_de_preco(client):
    client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": "CP-SIMULA", "nome_local": "x"}, headers=ADMIN)
    assert client.post("/api/v1/admin/carregadores/CP-SIMULA/simular-pagamento", json={"valor": 10}, headers=ADMIN).status_code == 422
    client.patch("/api/v1/admin/carregadores/CP-SIMULA", json={"preco_valor": 10, "preco_minutos": 20}, headers=ADMIN)
    with client.websocket_connect("/CP-SIMULA") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        r = client.post("/api/v1/admin/carregadores/CP-SIMULA/simular-pagamento", json={"valor": 15}, headers=ADMIN)
        assert r.json()["minutos"] == 30
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Accepted"})


def test_migracao_adiciona_coluna_nova_em_tabela_existente(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'velho.db'}")
    Table("carros", MetaData(), Column("id", Integer, primary_key=True)).create(engine)
    Base = declarative_base()

    class Carro(Base):  # noqa: F841 - registra o model novo
        __tablename__ = "carros"
        id = Column(Integer, primary_key=True)
        placa = Column(String, nullable=True)

    adicionar_colunas_faltando(engine, Base)
    assert "placa" in {c["name"] for c in inspect(engine).get_columns("carros")}


def _sessao_db(sessao_id: int) -> SessaoCarga:
    db = SessionLocal()
    try:
        return db.get(SessaoCarga, sessao_id)
    finally:
        db.close()


def test_cliente_com_token_proprio_cria_caixa_com_o_endereco_dele(client, monkeypatch):
    chamadas = []

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        chamadas.append((method, url, token, body))
        if url.endswith("/users/me"):
            if token == "APP_USR-ruim":
                raise pagamentos.HTTPException(status_code=502, detail="Mercado Pago retornou erro 401: invalid_token")
            return {"id": 777}
        if "/stores/search" in url or url.startswith("https://api.mercadopago.com/pos?"):
            return {"results": []}
        if url.endswith("/stores"):
            return {"id": "S9", "external_id": body["external_id"]}
        if url.endswith("/pos"):
            return {"id": "P9", "external_id": body["external_id"]}
        raise AssertionError(url)

    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)
    ruim = client.post("/api/v1/admin/clientes", json={"nome_empresa": "X", "mp_access_token": "APP_USR-ruim"}, headers=ADMIN)
    assert ruim.status_code == 422

    r = client.post(
        "/api/v1/admin/clientes",
        json={"nome_empresa": "Gira Kids", "mp_access_token": "APP_USR-bom", "endereco_rua": "Rua A", "endereco_numero": "10",
              "endereco_cidade": "Campinas", "endereco_estado": "Sao Paulo"},
        headers=ADMIN,
    )
    cliente = r.json()
    assert cliente["tem_token_mp"] and cliente["mp_user_id"] == "777" and "mp_access_token" not in cliente

    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-CLIENTE", "nome_local": "Garagem", "cliente_id": cliente["id"]}, headers=ADMIN)
    r = client.post("/api/v1/admin/carregadores/CP-CLIENTE/mercado-pago/caixa", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["cliente_nome"] == "Gira Kids" and r.json()["mp_pos_id"] == "P9"
    loja = next(body for method, url, token, body in chamadas if method == "POST" and url.endswith("/stores"))
    assert loja["location"]["city_name"] == "Campinas" and loja["location"]["street_name"] == "Rua A"
    assert all(token == "APP_USR-bom" for method, url, token, body in chamadas if "users/me" not in url)

    # Editar sem mandar token mantem o salvo.
    r = client.patch(f"/api/v1/admin/clientes/{cliente['id']}", json={"telefone": "1199"}, headers=ADMIN)
    assert r.json()["tem_token_mp"] and r.json()["carregadores"] == 1


def test_cliente_guarda_public_key_client_id_e_secret(client):
    r = client.post(
        "/api/v1/admin/clientes",
        json={"nome_empresa": "Com OAuth", "cliente_mercado_pago": True, "mp_public_key": "APP_USR-pub", "mp_client_id": "123", "mp_client_secret": "segredo"},
        headers=ADMIN,
    ).json()
    assert r["mp_public_key"] == "APP_USR-pub" and r["mp_client_id"] == "123" and r["tem_client_secret"]
    assert "segredo" not in str(r)
    # Editar sem mandar o secret mantem o salvo.
    r = client.patch(f"/api/v1/admin/clientes/{r['id']}", json={"mp_client_id": "456"}, headers=ADMIN).json()
    assert r["mp_client_id"] == "456" and r["tem_client_secret"]


def test_renomear_carregador_renomeia_loja_e_caixa_no_mp(client, monkeypatch):
    _carregador_com_caixa(client, "CP-NOME", "POS-NOME")
    chamadas = []

    def mp_falso(method, url, token, body=None, headers=None, timeout=30):
        chamadas.append((method, url, body))
        if method == "GET" and "/stores/" in url:
            return {"name": "Garagem GiraKids"}
        if method == "GET" and "/pos/" in url:
            return {"name": "Garagem GiraKids"}
        return {}

    monkeypatch.setattr(pagamentos, "mp_request", mp_falso)
    r = client.patch("/api/v1/admin/carregadores/CP-NOME", json={"nome_local": "Garagem GiraKids"}, headers=ADMIN).json()
    assert "aviso_mp" not in r
    puts = [(url, body) for method, url, body in chamadas if method == "PUT"]
    assert any(url.endswith("/stores/STORE-POS-NOME") and body == {"name": "Garagem GiraKids"} for url, body in puts)
    assert any(url.endswith("/pos/POS-NOME") and body == {"name": "Garagem GiraKids"} for url, body in puts)
    assert client.get("/api/v1/admin/carregadores/CP-NOME/mercado-pago/caixa", headers=ADMIN).json() == {"loja": "Garagem GiraKids", "caixa": "Garagem GiraKids"}
