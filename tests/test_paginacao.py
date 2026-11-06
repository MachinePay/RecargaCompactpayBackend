"""Recargas e historico paginados no servidor, com filtros."""
from datetime import timedelta

from app.db.session import SessionLocal
from app.models.carregador import Cobranca, HistoricoOperacao, SessaoCarga, agora
from tests.test_fluxo_carga import ADMIN, client  # noqa: F401 - fixture


def _criar(client, cp_id: str, cliente_id: int, quantas: int) -> None:
    client.post("/api/v1/admin/carregadores", json={"id_hardware": cp_id, "nome_local": cp_id, "cliente_id": cliente_id}, headers=ADMIN)
    db = SessionLocal()
    try:
        for i in range(quantas):
            cobranca = Cobranca(carregador_id=cp_id, minutos=10, valor=5, origem="caixa", status="aprovada", external_reference=f"PAG-{cp_id}-{i}")
            db.add(cobranca)
            db.flush()
            # Metade de 10 dias atras, metade de hoje; a ultima ainda carregando.
            criada = agora() - timedelta(days=10 if i % 2 else 0)
            status = "carregando" if i == quantas - 1 else "finalizada"
            db.add(SessaoCarga(carregador_id=cp_id, cobranca_id=cobranca.id, id_tag=f"T{i}", minutos_pagos=10, status=status, created_at=criada))
            db.add(HistoricoOperacao(carregador_id=cp_id, categoria="SESSAO" if i % 2 else "PAGAMENTO", descricao=f"evento {i}"))
        db.commit()
    finally:
        db.close()


def test_recargas_paginadas_com_filtros(client):
    dono = client.post("/api/v1/admin/usuarios", json={"nome": "Dono Pag", "email": "dono@pag.com", "senha": "segredo1", "role": "cliente"}, headers=ADMIN).json()
    outro = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Outro Pag", "cliente_mercado_pago": True}, headers=ADMIN).json()
    _criar(client, "CP-PAG", dono["cliente_id"], 30)
    _criar(client, "CP-PAG-OUTRO", outro["id"], 5)

    r = client.get("/api/v1/admin/recargas", params={"carregador_id": "CP-PAG", "por_pagina": 10}, headers=ADMIN).json()
    assert r["total"] == 30 and r["paginas"] == 3 and len(r["itens"]) == 10
    ids = [s["id"] for s in r["itens"]]
    assert ids == sorted(ids, reverse=True)
    terceira = client.get("/api/v1/admin/recargas", params={"carregador_id": "CP-PAG", "por_pagina": 10, "pagina": 3}, headers=ADMIN).json()
    assert len(terceira["itens"]) == 10 and not set(ids) & {s["id"] for s in terceira["itens"]}
    # Limite por pagina nao passa de 100.
    assert client.get("/api/v1/admin/recargas", params={"por_pagina": 5000}, headers=ADMIN).json()["por_pagina"] == 100

    ativas = client.get("/api/v1/admin/recargas", params={"carregador_id": "CP-PAG", "status": "ativas"}, headers=ADMIN).json()
    assert ativas["total"] == 1
    hoje = agora().date()
    recentes = client.get("/api/v1/admin/recargas", params={"carregador_id": "CP-PAG", "de": str(hoje - timedelta(days=2))}, headers=ADMIN).json()
    assert recentes["total"] == 15
    assert client.get("/api/v1/admin/recargas", params={"status": "xyz"}, headers=ADMIN).status_code == 422

    # Cliente ve so as recargas dos carregadores dele.
    token = client.post("/api/v1/auth/login", json={"email": "dono@pag.com", "senha": "segredo1"}).json()["token"]
    meu = client.get("/api/v1/admin/recargas", params={"por_pagina": 100}, headers={"Authorization": f"Bearer {token}"}).json()
    assert meu["total"] == 30 and {s["carregador_id"] for s in meu["itens"]} == {"CP-PAG"}

    h = client.get("/api/v1/admin/carregadores/CP-PAG/historico", params={"por_pagina": 7}, headers=ADMIN).json()
    assert h["total"] >= 30 and len(h["itens"]) == 7
    so_sessao = client.get("/api/v1/admin/carregadores/CP-PAG/historico", params={"categoria": "SESSAO"}, headers=ADMIN).json()
    assert so_sessao["total"] == 15 and {x["categoria"] for x in so_sessao["itens"]} == {"SESSAO"}



def test_listas_do_cadastro_paginadas_com_busca(client):
    from tests.test_fluxo_carga import CarregadorFake

    cid = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Rede Lista Zeta", "cliente_mercado_pago": True}, headers=ADMIN).json()["id"]
    for i in range(12):
        client.post("/api/v1/admin/carregadores", json={"id_hardware": f"CP-LST-{i:02d}", "nome_local": f"Posto Lista {i:02d}", "cliente_id": cid}, headers=ADMIN)

    with client.websocket_connect("/ocpp/CP-LST-03", subprotocols=["ocpp1.6"]) as ws:
        CarregadorFake(ws).ligar()
        r = client.get("/api/v1/admin/carregadores", params={"pagina": 1, "por_pagina": 5, "cliente_id": cid}, headers=ADMIN).json()
        assert r["total"] == 12 and r["paginas"] == 3 and len(r["itens"]) == 5
        assert r["contagem"]["todos"] == 12 and r["contagem"]["online"] == 1 and r["contagem"]["offline"] == 11
        assert r["resumo"]["online"] >= 1
        # Ordem por status: offline primeiro; descendo, o livre vem na frente.
        assert r["itens"][0]["online"] is False
        desc = client.get("/api/v1/admin/carregadores", params={"pagina": 1, "cliente_id": cid, "ordem": "status", "direcao": "desc"}, headers=ADMIN).json()
        assert desc["itens"][0]["id_hardware"] == "CP-LST-03"
        livres = client.get("/api/v1/admin/carregadores", params={"pagina": 1, "cliente_id": cid, "status": "online"}, headers=ADMIN).json()
        assert [c["id_hardware"] for c in livres["itens"]] == ["CP-LST-03"]

    achou = client.get("/api/v1/admin/carregadores", params={"pagina": 1, "busca": "lista 07"}, headers=ADMIN).json()
    assert [c["id_hardware"] for c in achou["itens"]] == ["CP-LST-07"]
    por_cliente = client.get("/api/v1/admin/carregadores", params={"pagina": 1, "busca": "zeta"}, headers=ADMIN).json()
    assert por_cliente["total"] == 12
    nomes = [c["nome_local"] for c in client.get("/api/v1/admin/carregadores", params={"pagina": 1, "cliente_id": cid, "ordem": "carregador", "direcao": "desc"}, headers=ADMIN).json()["itens"]]
    assert nomes[0] == "Posto Lista 11"
    # Sem pagina continua devolvendo a lista (mapa); opcoes e' so id e nome.
    assert isinstance(client.get("/api/v1/admin/carregadores", headers=ADMIN).json(), list)
    opcoes = client.get("/api/v1/admin/carregadores/opcoes", params={"cliente_id": cid}, headers=ADMIN).json()
    assert len(opcoes) == 12 and set(opcoes[0]) == {"id_hardware", "nome_local", "cliente_id", "cliente_nome"}

    clientes = client.get("/api/v1/admin/clientes", params={"pagina": 1, "busca": "zeta"}, headers=ADMIN).json()
    assert [c["id"] for c in clientes["itens"]] == [cid]
    assert any(c["id"] == cid for c in client.get("/api/v1/admin/clientes/opcoes", params={"sem_login": True}, headers=ADMIN).json())

    for i in range(3):
        client.post("/api/v1/admin/usuarios", json={"nome": f"Lista Pessoa {i}", "email": f"lista{i}@x.com", "senha": "segredo1", "role": "admin"}, headers=ADMIN)
    u = client.get("/api/v1/admin/usuarios", params={"busca": "lista pessoa", "por_pagina": 2}, headers=ADMIN).json()
    assert u["total"] == 3 and len(u["itens"]) == 2 and u["paginas"] == 2

    for nome in ("Lista V1", "Lista V2"):
        client.post("/api/v1/admin/vouchers", json={"nome": nome, "minutos": 10, "carregadores": ["CP-LST-00"]}, headers=ADMIN)
    v = client.get("/api/v1/admin/vouchers", params={"busca": "lista v"}, headers=ADMIN).json()
    assert [x["nome"] for x in v["itens"]] == ["Lista V2", "Lista V1"]
    client.post(f"/api/v1/admin/vouchers/{v['itens'][0]['id']}/cancelar", headers=ADMIN)
    validos = client.get("/api/v1/admin/vouchers", params={"busca": "lista v", "situacao": "validos"}, headers=ADMIN).json()
    assert [x["nome"] for x in validos["itens"]] == ["Lista V1"]
