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
