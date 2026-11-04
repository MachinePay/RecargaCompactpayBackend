"""Relatorio da tela Gestao."""
from datetime import timedelta

from app.db.session import SessionLocal
from app.models.carregador import Cobranca, SessaoCarga, agora
from tests.test_fluxo_carga import ADMIN, cliente_teste, client  # noqa: F401 - fixture


def test_relatorio_soma_por_dia_hora_ranking_e_metodo(client):
    for cp in ("CP-REL-A", "CP-REL-B"):
        client.post("/api/v1/admin/carregadores", json={"cliente_id": cliente_teste(client), "id_hardware": cp, "nome_local": cp}, headers=ADMIN)
    agora_utc = agora()
    db = SessionLocal()
    try:
        def venda(cp, valor, metodo, origem="caixa", estornada=False, dias=0):
            c = Cobranca(carregador_id=cp, minutos=20, valor=valor, origem=origem, status="aprovada", metodo=metodo,
                         external_reference=f"EV:{cp}:t{valor}{metodo}{dias}{origem}", pago_em=agora_utc - timedelta(days=dias),
                         estornada_em=agora_utc if estornada else None)
            db.add(c)
            db.flush()
            return c

        a = venda("CP-REL-A", 10, "pix")
        venda("CP-REL-A", 20, "credit_card")
        venda("CP-REL-B", 5, "pix")
        venda("CP-REL-B", 7, "pix", estornada=True)
        venda("CP-REL-B", 99, "simulado", origem="simulado")
        venda("CP-REL-A", 30, "pix", dias=8)  # fora dos 7 dias: entra no periodo anterior
        db.add(SessaoCarga(carregador_id="CP-REL-A", cobranca_id=a.id, id_tag="x", minutos_pagos=20, status="finalizada",
                           inicio=agora_utc - timedelta(minutes=30), fim_real=agora_utc - timedelta(minutes=10),
                           meter_inicio_wh=0, meter_atual_wh=2500))
        db.commit()
    finally:
        db.close()

    r = client.get("/api/v1/admin/relatorio?periodo=7d", headers=ADMIN).json()
    ranking = {x["id_hardware"]: x for x in r["ranking"] if x["id_hardware"].startswith("CP-REL")}
    assert ranking["CP-REL-A"]["faturamento"] == 30 and ranking["CP-REL-A"]["vendas"] == 2
    assert ranking["CP-REL-A"]["energia_kwh"] == 2.5 and ranking["CP-REL-A"]["ocupacao_pct"] > 0
    assert ranking["CP-REL-B"]["faturamento"] == 5 and ranking["CP-REL-B"]["estornos_qtd"] == 1
    assert r["periodo"]["dias"] == 7 and len(r["por_dia"]) == 7
    assert sum(d["faturamento"] for d in r["por_dia"]) >= 35
    assert sum(c["vendas"] for linha in r["grade"] for c in linha) >= 3
    assert any(m["metodo"] == "pix" for m in r["por_metodo"])
    assert r["anterior"]["faturamento"] >= 30

    com_simulados = client.get("/api/v1/admin/relatorio?periodo=7d&incluir_simulados=true&carregador_id=CP-REL-B", headers=ADMIN).json()
    assert com_simulados["totais"]["faturamento"] == 104
    assert client.get("/api/v1/admin/relatorio?periodo=xyz", headers=ADMIN).status_code == 422
