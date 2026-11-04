"""Historico de quedas e saude dos carregadores (telas Quedas e Saude, no
espirito das HistoricoQuedas e SaudeMaquinas da CompactPay).

Queda = periodo entre "Carregador desconectou" e o "conectou" seguinte no
historico_operacoes. Reinicio do servidor derruba todos de uma vez; esses
aparecem como queda curta de todos ao mesmo tempo.
"""
from datetime import timedelta

from app.models.carregador import Carregador, Cobranca, HistoricoOperacao, SessaoCarga, agora
from app.services import ocpp_server

CONECTOU = "Carregador conectou no servidor OCPP"
DESCONECTOU = "Carregador desconectou do servidor OCPP"


def quedas(db, carregadores: list[Carregador], de, ate) -> list[dict]:
    """Quedas que comecaram no periodo (a de agora aparece como em andamento)."""
    ids = [c.id_hardware for c in carregadores]
    nomes = {c.id_hardware: c for c in carregadores}
    eventos = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.carregador_id.in_(ids),
            HistoricoOperacao.descricao.in_((CONECTOU, DESCONECTOU)),
            HistoricoOperacao.created_at >= de - timedelta(days=1),
            HistoricoOperacao.created_at < ate,
        )
        .order_by(HistoricoOperacao.carregador_id, HistoricoOperacao.id)
        .all()
    )
    abertas: dict[str, object] = {}
    lista = []
    momento = agora()
    for evento in eventos:
        if evento.descricao == DESCONECTOU:
            abertas[evento.carregador_id] = evento.created_at
        elif evento.carregador_id in abertas:
            inicio = abertas.pop(evento.carregador_id)
            lista.append((evento.carregador_id, inicio, evento.created_at))
    for carregador_id, inicio in abertas.items():
        if not ocpp_server.esta_conectado(carregador_id):
            lista.append((carregador_id, inicio, None))
    resposta = []
    for carregador_id, inicio, fim in lista:
        if inicio < de:
            continue
        c = nomes[carregador_id]
        resposta.append(
            {
                "carregador_id": carregador_id,
                "nome_local": c.nome_local,
                "cliente_nome": c.cliente.nome_empresa if c.cliente else None,
                "inicio": inicio,
                "fim": fim,
                "em_andamento": fim is None,
                "duracao_min": round(((fim or momento) - inicio).total_seconds() / 60, 1),
            }
        )
    resposta.sort(key=lambda q: q["inicio"], reverse=True)
    return resposta


def resumo_quedas(lista: list[dict], carregadores: list[Carregador]) -> list[dict]:
    por: dict[str, dict] = {
        c.id_hardware: {"carregador_id": c.id_hardware, "nome_local": c.nome_local, "quedas": 0, "minutos_fora": 0.0, "maior_min": 0.0}
        for c in carregadores
    }
    for q in lista:
        item = por[q["carregador_id"]]
        item["quedas"] += 1
        item["minutos_fora"] = round(item["minutos_fora"] + q["duracao_min"], 1)
        item["maior_min"] = max(item["maior_min"], q["duracao_min"])
    return sorted(por.values(), key=lambda i: (-i["quedas"], -i["minutos_fora"], i["nome_local"]))


def _alerta(nivel: str, texto: str, dica: str = "") -> dict:
    return {"nivel": nivel, "texto": texto, "dica": dica}


def saude(db, carregadores: list[Carregador]) -> dict:
    momento = agora()
    semana = momento - timedelta(days=7)
    ids = [c.id_hardware for c in carregadores]
    lista_quedas = quedas(db, carregadores, semana, momento + timedelta(minutes=1))
    quedas_7d: dict[str, int] = {}
    for q in lista_quedas:
        quedas_7d[q["carregador_id"]] = quedas_7d.get(q["carregador_id"], 0) + 1
    falhas: dict[str, int] = {}
    for s in db.query(SessaoCarga).filter(SessaoCarga.carregador_id.in_(ids), SessaoCarga.status == "falhou", SessaoCarga.created_at >= semana).all():
        falhas[s.carregador_id] = falhas.get(s.carregador_id, 0) + 1
    estornos: dict[str, int] = {}
    for c in db.query(Cobranca).filter(Cobranca.carregador_id.in_(ids), Cobranca.estorno_erro.isnot(None), Cobranca.estornada_em.is_(None)).all():
        estornos[c.carregador_id] = estornos.get(c.carregador_id, 0) + 1

    itens = []
    for c in carregadores:
        online = ocpp_server.esta_conectado(c.id_hardware)
        alertas = []
        if not c.ativo:
            alertas.append(_alerta("info", "Carregador inativo", "Recusa a conexao e nao vende ate ser ativado em Editar."))
        elif not online:
            fora = momento - c.ultimo_sinal if c.ultimo_sinal else None
            alertas.append(
                _alerta(
                    "critico",
                    f"Offline ha {int(fora.total_seconds() // 3600)}h{int(fora.total_seconds() % 3600 // 60):02d}" if fora else "Nunca conectou",
                    "Confira energia e Wi-Fi do local. Se nunca conectou, veja o Diagnostico.",
                )
            )
        if c.erro_conector or c.status_conector == "Faulted":
            alertas.append(_alerta("critico", f"Conector com defeito ({c.erro_conector or 'Faulted'})", "Veja a tela do carregador e o manual."))
        if estornos.get(c.id_hardware):
            alertas.append(_alerta("critico", f"{estornos[c.id_hardware]} estorno(s) falharam", "Estornar manualmente no banco (veja Recargas)."))
        if quedas_7d.get(c.id_hardware, 0) >= 5:
            alertas.append(_alerta("aviso", f"{quedas_7d[c.id_hardware]} quedas em 7 dias", "Wi-Fi instavel: aproxime o roteador ou use um repetidor."))
        if falhas.get(c.id_hardware):
            alertas.append(_alerta("aviso", f"{falhas[c.id_hardware]} recarga(s) que nao comecaram em 7 dias", "Pagou e o carro nao carregou: veja as Recargas."))
        if not c.cliente_id:
            alertas.append(_alerta("aviso", "Sem cliente", "Vincule a um cliente em Editar para poder vender."))
        if not (c.preco_valor and c.preco_minutos):
            alertas.append(_alerta("aviso", "Sem regra de preco", "Defina quanto tempo cada real libera."))
        banco = c.banco_pagamento or "mercado_pago"
        if banco == "mercado_pago" and not c.mp_pos_id:
            alertas.append(_alerta("aviso", "Sem caixa do Mercado Pago", "Crie a loja e o caixa na pagina do carregador."))
        if banco == "sumup" and not (c.sumup_reader_id or c.sumup_device_code):
            alertas.append(_alerta("aviso", "Sem maquininha SumUp", "Pareie ou informe o serial da maquininha."))
        if c.latitude is None:
            alertas.append(_alerta("info", "Sem localizacao no mapa", "Marque o ponto na pagina do carregador."))
        niveis = {a["nivel"] for a in alertas}
        itens.append(
            {
                "id_hardware": c.id_hardware,
                "nome_local": c.nome_local,
                "cliente_nome": c.cliente.nome_empresa if c.cliente else None,
                "online": online,
                "ultimo_sinal": c.ultimo_sinal,
                "firmware_version": c.firmware_version,
                "quedas_7d": quedas_7d.get(c.id_hardware, 0),
                "falhas_7d": falhas.get(c.id_hardware, 0),
                "status": "critico" if "critico" in niveis else "aviso" if "aviso" in niveis else "ok",
                "alertas": alertas,
            }
        )
    ordem = {"critico": 0, "aviso": 1, "ok": 2}
    itens.sort(key=lambda i: (ordem[i["status"]], i["nome_local"]))
    return {
        "resumo": {
            "total": len(itens),
            "online": sum(1 for i in itens if i["online"]),
            "critico": sum(1 for i in itens if i["status"] == "critico"),
            "aviso": sum(1 for i in itens if i["status"] == "aviso"),
            "ok": sum(1 for i in itens if i["status"] == "ok"),
        },
        "carregadores": itens,
    }
