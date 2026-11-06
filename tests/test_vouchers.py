"""Voucher de recarga gratis: admin em qualquer carregador, cliente so nos dele."""
from datetime import datetime, timedelta, timezone

from tests.test_fluxo_carga import ADMIN, CarregadorFake, client  # noqa: F401 - fixture

CELULAR = "celular-voucher-0001"


def _cliente_com_login(client, nome: str, email: str) -> tuple[int, dict]:
    u = client.post("/api/v1/admin/usuarios", json={"nome": nome, "email": email, "senha": "segredo1", "role": "cliente"}, headers=ADMIN).json()
    token = client.post("/api/v1/auth/login", json={"email": email, "senha": "segredo1"}).json()["token"]
    return u["cliente_id"], {"Authorization": f"Bearer {token}"}


def test_voucher_libera_recarga_e_o_link_some(client):
    dono, cab = _cliente_com_login(client, "Dono Voucher", "dono@voucher.com")
    outro, _ = _cliente_com_login(client, "Outro Voucher", "outro@voucher.com")
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-VOU", "nome_local": "Garagem", "cliente_id": dono}, headers=ADMIN)
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-VOU-OUTRO", "nome_local": "Outro", "cliente_id": outro}, headers=ADMIN)

    # Cliente nao cria voucher em carregador de outro cliente; admin pode.
    corpo = {"nome": "Cortesia", "minutos": 30, "carregadores": ["CP-VOU-OUTRO"]}
    assert client.post("/api/v1/admin/vouchers", json=corpo, headers=cab).status_code == 404
    assert client.post("/api/v1/admin/vouchers", json=corpo, headers=ADMIN).status_code == 200
    # Validade no passado e' recusada.
    passado = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert client.post("/api/v1/admin/vouchers", json={**corpo, "carregadores": ["CP-VOU"], "expira_em": passado}, headers=cab).status_code == 422

    futuro = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    v = client.post("/api/v1/admin/vouchers", json={"nome": "Cortesia Joao", "minutos": 45, "expira_em": futuro, "carregadores": ["CP-VOU"]}, headers=cab).json()
    assert v["situacao"] == "ativo" and v["cliente_id"] == dono and v["usos"] == 0
    # O cliente ve so os vouchers dele.
    assert [x["id"] for x in client.get("/api/v1/admin/vouchers", headers=cab).json()] == [v["id"]]

    with client.websocket_connect("/ocpp/CP-VOU", subprotocols=["ocpp1.6"]) as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        link = client.get(f"/api/v1/publico/voucher/{v['codigo']}").json()
        assert link["minutos"] == 45 and [c["id"] for c in link["carregadores"]] == ["CP-VOU"]
        assert link["carregadores"][0]["situacao"] == "livre"

        r = client.post(f"/api/v1/publico/voucher/{v['codigo']}/usar", json={"carregador_id": "CP-VOU-OUTRO", "dispositivo": CELULAR})
        assert r.status_code == 404
        r = client.post(f"/api/v1/publico/voucher/{v['codigo']}/usar", json={"carregador_id": "CP-VOU", "dispositivo": CELULAR})
        assert r.status_code == 200, r.text
        recarga = r.json()
        assert recarga["status"] == "aprovada" and recarga["valor"] == 0 and recarga["minutos"] == 45
        start = cp.receber_chamada("RemoteStartTransaction")
        cp.responder(start, {"status": "Accepted"})

        # Usado: o link some e o painel mostra esgotado.
        assert client.get(f"/api/v1/publico/voucher/{v['codigo']}").status_code == 404
        assert client.get("/api/v1/admin/vouchers", headers=cab).json()[0]["situacao"] == "esgotado"
        # A recarga aparece para o celular que usou, como um Pix.
        assert client.get(f"/api/v1/publico/cobrancas/{recarga['id']}", params={"dispositivo": CELULAR}).json()["sessao"]


def test_voucher_cancelado_some(client):
    v = client.post("/api/v1/admin/vouchers", json={"nome": "X", "minutos": 10, "carregadores": ["CP-VOU-OUTRO"]}, headers=ADMIN)
    if v.status_code == 404:  # rodando sozinho: cria o carregador
        cid = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono X", "cliente_mercado_pago": True}, headers=ADMIN).json()["id"]
        client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-VOU-OUTRO", "nome_local": "Outro", "cliente_id": cid}, headers=ADMIN)
        v = client.post("/api/v1/admin/vouchers", json={"nome": "X", "minutos": 10, "carregadores": ["CP-VOU-OUTRO"]}, headers=ADMIN)
    v = v.json()
    assert client.get(f"/api/v1/publico/voucher/{v['codigo']}").status_code == 200
    assert client.post(f"/api/v1/admin/vouchers/{v['id']}/cancelar", headers=ADMIN).json()["situacao"] == "cancelado"
    assert client.get(f"/api/v1/publico/voucher/{v['codigo']}").status_code == 404
