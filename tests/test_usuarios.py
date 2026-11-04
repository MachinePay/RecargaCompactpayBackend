"""Login com usuario e senha, acesso por cliente, quedas e saude."""
from datetime import timedelta

from app.db.session import SessionLocal
from app.models.carregador import HistoricoOperacao, agora
from tests.test_fluxo_carga import ADMIN, CarregadorFake, client  # noqa: F401 - fixture


def _entrar(client, email, senha):
    r = client.post("/api/v1/auth/login", json={"email": email, "senha": senha})
    return r


def test_cliente_ve_so_os_proprios_carregadores_e_nao_administra(client):
    a = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono A", "cliente_mercado_pago": True}, headers=ADMIN).json()
    b = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono B", "cliente_mercado_pago": True}, headers=ADMIN).json()
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-DONO-A", "nome_local": "A", "cliente_id": a["id"]}, headers=ADMIN)
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-DONO-B", "nome_local": "B", "cliente_id": b["id"]}, headers=ADMIN)

    # Usuario de cliente precisa de cliente; senha curta e' recusada.
    assert client.post("/api/v1/admin/usuarios", json={"nome": "X", "email": "x@x.com", "senha": "123456", "role": "cliente"}, headers=ADMIN).status_code == 422
    assert client.post("/api/v1/admin/usuarios", json={"nome": "X", "email": "x@x.com", "senha": "123", "role": "cliente", "cliente_id": a["id"]}, headers=ADMIN).status_code == 422
    u = client.post("/api/v1/admin/usuarios", json={"nome": "Ana", "email": "Ana@DonoA.com", "senha": "segredo1", "role": "cliente", "cliente_id": a["id"]}, headers=ADMIN)
    assert u.status_code == 200 and u.json()["email"] == "ana@donoa.com" and "senha_hash" not in u.json()
    assert client.post("/api/v1/admin/usuarios", json={"nome": "Outra", "email": "ana@donoa.com", "senha": "segredo1", "role": "cliente", "cliente_id": a["id"]}, headers=ADMIN).status_code == 409

    assert _entrar(client, "ana@donoa.com", "errada").status_code == 401
    login = _entrar(client, "ANA@donoa.com", "segredo1").json()
    assert login["usuario"]["role"] == "cliente"
    cab = {"Authorization": f"Bearer {login['token']}"}

    ids = [c["id_hardware"] for c in client.get("/api/v1/admin/carregadores", headers=cab).json()]
    assert ids == ["CP-DONO-A"]
    assert client.get("/api/v1/admin/carregadores/CP-DONO-B", headers=cab).status_code == 404
    assert [c["id"] for c in client.get("/api/v1/admin/clientes", headers=cab).json()] == [a["id"]]
    assert client.get("/api/v1/admin/relatorio?periodo=7d", headers=cab).status_code == 200
    assert client.get("/api/v1/auth/me", headers=cab).json()["nome"] == "Ana"
    # Acoes de administrador sao bloqueadas.
    assert client.patch("/api/v1/admin/carregadores/CP-DONO-A", json={"nome_local": "Z"}, headers=cab).status_code == 403
    assert client.get("/api/v1/admin/usuarios", headers=cab).status_code == 403
    assert client.get("/api/v1/admin/whatsapp", headers=cab).status_code == 403
    assert client.get("/api/v1/admin/carregadores", headers={"Authorization": "Bearer lixo"}).status_code == 401

    # Usuario desativado perde o acesso na hora.
    client.patch(f"/api/v1/admin/usuarios/{u.json()['id']}", json={"ativo": False}, headers=ADMIN)
    assert client.get("/api/v1/admin/carregadores", headers=cab).status_code == 401

    # Admin por usuario tambem administra.
    client.post("/api/v1/admin/usuarios", json={"nome": "Chefe", "email": "chefe@recarga.com", "senha": "segredo2", "role": "admin"}, headers=ADMIN)
    adm = {"Authorization": f"Bearer {_entrar(client, 'chefe@recarga.com', 'segredo2').json()['token']}"}
    assert client.get("/api/v1/admin/usuarios", headers=adm).status_code == 200


def test_quedas_e_saude(client):
    cliente = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono Q", "cliente_mercado_pago": True}, headers=ADMIN).json()
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-QUEDA2", "nome_local": "Q", "cliente_id": cliente["id"]}, headers=ADMIN)
    momento = agora()
    db = SessionLocal()
    try:
        for minutos, descricao in [(90, "desconectou"), (60, "conectou"), (30, "desconectou")]:
            db.add(HistoricoOperacao(carregador_id="CP-QUEDA2", categoria="DISPOSITIVO", descricao=f"Carregador {descricao} {'do' if descricao == 'desconectou' else 'no'} servidor OCPP", created_at=momento - timedelta(minutes=minutos)))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/v1/admin/quedas?periodo=7d&carregador_id=CP-QUEDA2", headers=ADMIN).json()
    assert len(r["quedas"]) == 2
    assert r["quedas"][0]["em_andamento"] and r["quedas"][1]["duracao_min"] == 30
    assert r["por_carregador"][0]["quedas"] == 2

    s = client.get("/api/v1/admin/saude", headers=ADMIN).json()
    item = next(c for c in s["carregadores"] if c["id_hardware"] == "CP-QUEDA2")
    assert item["status"] == "critico" and any("Offline" in a["texto"] or "Nunca" in a["texto"] for a in item["alertas"])
    assert any("preco" in a["texto"] for a in item["alertas"])
