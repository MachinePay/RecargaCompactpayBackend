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

    # Senha curta e' recusada.
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


def test_excluir_carregador_apaga_ou_arquiva(client):
    cliente = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono Ex", "cliente_mercado_pago": True}, headers=ADMIN).json()
    novo = {"nome_local": "Ex", "cliente_id": cliente["id"]}
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-EX-VAZIO", **novo}, headers=ADMIN)
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-EX-VENDEU", **novo}, headers=ADMIN)
    client.patch("/api/v1/admin/carregadores/CP-EX-VENDEU", json={"preco_valor": 10, "preco_minutos": 20}, headers=ADMIN)

    # Sem historico: apaga de vez.
    assert client.delete("/api/v1/admin/carregadores/CP-EX-VAZIO", headers=ADMIN).json()["modo"] == "apagado"
    assert client.get("/api/v1/admin/carregadores/CP-EX-VAZIO", headers=ADMIN).status_code == 404

    with client.websocket_connect("/CP-EX-VENDEU") as ws:
        cp = CarregadorFake(ws)
        cp.ligar()
        client.post("/api/v1/admin/carregadores/CP-EX-VENDEU/simular-pagamento", json={"valor": 10}, headers=ADMIN)
        cp.responder(cp.receber_chamada("RemoteStartTransaction"), {"status": "Accepted"})
        # Com recarga em andamento nao exclui.
        assert client.delete("/api/v1/admin/carregadores/CP-EX-VENDEU", headers=ADMIN).status_code == 409
        sessao = client.get("/api/v1/admin/carregadores/CP-EX-VENDEU", headers=ADMIN).json()["sessao_ativa"]
        client.post(f"/api/v1/admin/sessoes/{sessao['id']}/encerrar", headers=ADMIN)

    # Com vendas: arquiva (some da lista, recusa conexao, vendas ficam).
    assert client.delete("/api/v1/admin/carregadores/CP-EX-VENDEU", headers=ADMIN).json()["modo"] == "arquivado"
    assert "CP-EX-VENDEU" not in [c["id_hardware"] for c in client.get("/api/v1/admin/carregadores", headers=ADMIN).json()]
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/CP-EX-VENDEU") as ws:
            ws.receive_json()
    rel = client.get("/api/v1/admin/relatorio?periodo=hoje&incluir_simulados=true", headers=ADMIN).json()
    # Encerrada antes de comecar: foi estornada, mas continua no historico.
    assert any(r["id_hardware"] == "CP-EX-VENDEU" and r["vendas"] + r["estornos_qtd"] == 1 for r in rel["ranking"])

    # O mesmo ID pode ser cadastrado de novo, sem a configuracao antiga.
    r = client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-EX-VENDEU", **novo}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["preco_valor"] is None


def test_cadastro_do_cliente_cria_o_login(client):
    c = client.post(
        "/api/v1/admin/clientes",
        json={"nome_empresa": "Dono Login", "cliente_mercado_pago": True, "acesso_email": "Dono@Login.com", "acesso_senha": "segredo1"},
        headers=ADMIN,
    ).json()
    assert c["acessos"] == ["dono@login.com"]
    assert any(u["email"] == "dono@login.com" and u["cliente_id"] == c["id"] for u in client.get("/api/v1/admin/usuarios", params={"busca": "dono@login"}, headers=ADMIN).json()["itens"])
    assert _entrar(client, "dono@login.com", "segredo1").status_code == 200
    # Editar com senha nova troca a senha do mesmo login.
    client.patch(f"/api/v1/admin/clientes/{c['id']}", json={"acesso_email": "dono@login.com", "acesso_senha": "outra123"}, headers=ADMIN)
    assert _entrar(client, "dono@login.com", "outra123").status_code == 200
    # Senha curta e e-mail de outro cliente sao recusados.
    assert client.post("/api/v1/admin/clientes", json={"nome_empresa": "X", "acesso_email": "x@x.com", "acesso_senha": "123"}, headers=ADMIN).status_code == 422
    assert client.post("/api/v1/admin/clientes", json={"nome_empresa": "Y", "acesso_email": "dono@login.com", "acesso_senha": "segredo1"}, headers=ADMIN).status_code == 409


def test_novo_usuario_cliente_cria_o_cliente_dono(client):
    u = client.post("/api/v1/admin/usuarios", json={"nome": "Posto Novo", "email": "posto@novo.com", "senha": "segredo1", "role": "cliente"}, headers=ADMIN).json()
    assert u["cliente_nome"] == "Posto Novo" and u["cliente_id"]
    cliente = next(c for c in client.get("/api/v1/admin/clientes", headers=ADMIN).json() if c["id"] == u["cliente_id"])
    assert cliente["acessos"] == ["posto@novo.com"] and cliente["email_contato"] == "posto@novo.com"
    # Administrador nao ganha cliente.
    adm = client.post("/api/v1/admin/usuarios", json={"nome": "Adm", "email": "adm@novo.com", "senha": "segredo1", "role": "admin"}, headers=ADMIN).json()
    assert adm["cliente_id"] is None
