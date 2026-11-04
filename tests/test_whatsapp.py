"""Avisos de WhatsApp (Evolution API), igual a CompactPay."""
from datetime import timedelta

from app.db.session import SessionLocal
from app.models.carregador import Carregador, agora
from app.models.whatsapp import WhatsappCliente, WhatsappConfig
from app.services import whatsapp_alertas as wa
from tests.test_fluxo_carga import ADMIN, CarregadorFake, client  # noqa: F401 - fixture


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
    cliente = client.post("/api/v1/admin/clientes", json={"nome_empresa": "Rede WA", "telefone": "(11) 98888-7777", "cliente_mercado_pago": True}, headers=ADMIN).json()
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
