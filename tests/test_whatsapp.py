"""Avisos de WhatsApp (Evolution API), igual a CompactPay."""
from datetime import timedelta

from app.db.session import SessionLocal
from app.models.carregador import Carregador, agora
from app.models.whatsapp import WhatsappCliente, WhatsappConfig
from app.services import whatsapp_alertas as wa
from tests.test_fluxo_carga import ADMIN, CarregadorFake, client  # noqa: F401 - fixture
from tests.conftest import MP_TESTE


def test_avisa_offline_uma_vez_voltou_e_resumo(client, monkeypatch):
    enviados = []

    def evolution_falsa(config, caminho, method="GET", body=None):
        if caminho.startswith("/instance/connectionState/"):
            return {"instance": {"state": "open"}}
        if caminho.startswith("/message/sendText/"):
            enviados.append((body["number"], body["text"]))
            return {}
        raise AssertionError(caminho)

    monkeypatch.setattr(wa, "_chamar", evolution_falsa)
    cliente = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Rede WA", "telefone": "(11) 98888-7777", "cliente_mercado_pago": True, **MP_TESTE}, headers=ADMIN).json()
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-WA", "nome_local": "Posto WA", "cliente_id": cliente["id"]}, headers=ADMIN)

    # Ativar sem Evolution configurada e' recusado.
    assert client.put("/api/v1/admin/whatsapp/config", json={"ativo": True}, headers=ADMIN).status_code == 422
    tela = client.put(
        "/api/v1/admin/whatsapp/config",
        json={"evolution_url": "https://evo.exemplo/", "api_key": "chave-secreta-123", "instancia": "recarga", "ativo": True, "minutos_offline": 30},
        headers=ADMIN,
    ).json()
    assert tela["config"]["ativo"] and tela["config"]["evolution_url"] == "https://evo.exemplo" and "chave-secreta-123" not in str(tela)
    client.put(
        f"/api/v1/admin/whatsapp/clientes/{cliente['id']}",
        json={"ativo": True, "recebe_voltou": True, "recebe_lembrete": True, "resumo_diario": True, "resumo_horario": "00:00"},
        headers=ADMIN,
    )

    db = SessionLocal()
    try:
        # Caiu ha 40 min, depois de ligar o aviso.
        db.get(WhatsappConfig, 1).ativado_em = agora() - timedelta(hours=2)
        db.get(WhatsappCliente, cliente["id"]).ativado_em = agora() - timedelta(hours=2)
        db.get(Carregador, "CP-WA").ultimo_sinal = agora() - timedelta(minutes=40)
        db.commit()
        resultado = wa.verificar_e_enviar(db)
        assert resultado["offline"] == 1 and resultado["resumo"] == 1
        assert enviados[0][0] == "5511988887777" and "Posto WA" in enviados[0][1]
        # Mesma queda nao avisa de novo; resumo so uma vez por dia.
        assert wa.verificar_e_enviar(db)["offline"] == 0
        assert len(enviados) == 2
    finally:
        db.close()

    with client.websocket_connect("/CP-WA") as ws:
        CarregadorFake(ws).ligar()
        db = SessionLocal()
        try:
            assert wa.verificar_e_enviar(db)["voltou"] == 1
        finally:
            db.close()
    assert "Voltaram" in enviados[-1][1]
    historico = client.get("/api/v1/admin/whatsapp", headers=ADMIN).json()["envios"]
    assert {e["tipo"] for e in historico} >= {"offline", "resumo", "voltou"}


def test_teste_para_qualquer_numero_contadores_e_clientes_paginados(client, monkeypatch):
    enviados = []

    def evolution_falsa(config, caminho, method="GET", body=None):
        if caminho.startswith("/instance/connectionState/"):
            return {"instance": {"state": "open"}}
        if caminho.startswith("/message/sendText/"):
            enviados.append(body["number"])
            return {}
        raise AssertionError(caminho)

    monkeypatch.setattr(wa, "_chamar", evolution_falsa)
    client.put("/api/v1/admin/whatsapp/config", json={"evolution_url": "https://evo.exemplo", "api_key": "chave-1234567", "instancia": "recarga"}, headers=ADMIN)
    assert client.post("/api/v1/admin/whatsapp/teste", json={"telefone": "123"}, headers=ADMIN).status_code == 502
    r = client.post("/api/v1/admin/whatsapp/teste", json={"telefone": "(21) 99876-5432"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["telefone"] == "5521998765432" and enviados[-1] == "5521998765432"
    tela = client.get("/api/v1/admin/whatsapp", headers=ADMIN).json()
    assert tela["contadores"]["enviados_24h"] >= 1 and "clientes" not in tela

    for i in range(3):
        client.post("/api/v1/admin/clientes", json={"nome_empresa": f"Rede Zap {i}", "telefone": f"(11) 97777-000{i}", "cliente_mercado_pago": True, **MP_TESTE}, headers=ADMIN)
    pagina = client.get("/api/v1/admin/whatsapp/clientes", params={"busca": "rede zap", "por_pagina": 2}, headers=ADMIN).json()
    assert pagina["total"] == 3 and len(pagina["itens"]) == 2 and pagina["itens"][0]["telefones_validos"] == ["5511977770000"]


def test_offline_vai_para_todo_dono_e_estorno_recusado_avisa(client, monkeypatch):
    from app.models.carregador import Cobranca
    from app.services import pagamentos

    enviados = []

    def evolution_falsa(config, caminho, method="GET", body=None):
        if caminho.startswith("/instance/connectionState/"):
            return {"instance": {"state": "open"}}
        if caminho.startswith("/message/sendText/"):
            enviados.append((body["number"], body["text"]))
            return {}
        raise AssertionError(caminho)

    monkeypatch.setattr(wa, "_chamar", evolution_falsa)
    client.put(
        "/api/v1/admin/whatsapp/config",
        json={"evolution_url": "https://evo.exemplo", "api_key": "chave-1234567", "instancia": "gk", "ativo": True, "minutos_offline": 30},
        headers=ADMIN,
    )
    # Dono sem nada marcado na tela WhatsApp: recebe o aviso de queda mesmo assim.
    dono = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Dono Sempre", "telefone": "(31) 98888-1111", "cliente_mercado_pago": True, **MP_TESTE}, headers=ADMIN).json()
    client.post("/api/v1/admin/carregadores", json={"id_hardware": "CP-SEMPRE", "nome_local": "Posto Sempre", "cliente_id": dono["id"]}, headers=ADMIN)
    db = SessionLocal()
    try:
        db.get(WhatsappConfig, 1).ativado_em = agora() - timedelta(hours=2)
        db.get(Carregador, "CP-SEMPRE").ultimo_sinal = agora() - timedelta(minutes=45)
        db.commit()
        wa.verificar_e_enviar(db)
        assert any(numero == "5531988881111" and "Posto Sempre" in texto for numero, texto in enviados)

        # Estorno recusado pelo banco: o dono fica sabendo no WhatsApp.
        def mp_recusa(*args, **kwargs):
            from fastapi import HTTPException

            raise HTTPException(status_code=502, detail="refund not allowed")

        monkeypatch.setattr(pagamentos, "mp_request", mp_recusa)
        monkeypatch.setattr(wa, "avisar_estorno_falhou", lambda cobranca_id, valor, motivo: wa._avisar_estorno(cobranca_id, valor, motivo))
        cobranca = Cobranca(carregador_id="CP-SEMPRE", minutos=30, valor=15, origem="caixa", metodo="pix", status="aprovada",
                            external_reference="EV:CP-SEMPRE:estorno", mp_payment_id="555", pago_em=agora())
        db.add(cobranca)
        db.commit()
        assert not pagamentos.estornar_cobranca(db, cobranca)
        aviso = enviados[-1]
        assert aviso[0] == "5531988881111" and "R$ 15,00" in aviso[1] and "refund not allowed" in aviso[1]
    finally:
        db.close()
