"""Cobranca por kWh (pre-pago com devolucao da sobra).

O motorista paga um valor que vira energia pelo preco do carregador (ex.:
R$ 30 a R$ 2,00/kWh = 15 kWh). O OCPP 1.6 nao tem "limite de kWh" na
transacao, entao quem para e' o servidor: o worker de sessoes compara o
medidor (Energy.Active.Import.Register do MeterValues, menos o meterStart) com
a energia paga e manda o RemoteStop ao chegar. A recarga tambem tem um teto de
tempo (minutos_pagos), para o carro que puxa pouco nao ocupar a vaga.

No fim, o acerto calcula o que foi usado e devolve o resto no mesmo meio de
pagamento (estorno parcial no Mercado Pago ou na SumUp). Com mais de um
pagamento na mesma recarga (maquininha do caixa com o carro carregando), o
consumo e' descontado do primeiro pagamento, depois do segundo, e assim por
diante.
"""
import logging

from app.core.config import settings
from app.models.carregador import Carregador, Cobranca, SessaoCarga, agora
from app.services.ocpp_handlers import energia_kwh, registrar_historico


def cobra_por_kwh(carregador: Carregador | None) -> bool:
    return carregador is not None and carregador.modo_cobranca == "kwh"


def teto_minutos(carregador: Carregador) -> int:
    return carregador.kwh_tempo_maximo_min or settings.KWH_TEMPO_MAXIMO_MINUTOS


def kwh_por_valor(carregador: Carregador, valor: float) -> float:
    """Energia que o valor compra, arredondada para baixo em 0,01 kWh."""
    if not carregador.preco_kwh or carregador.preco_kwh <= 0 or valor <= 0:
        return 0.0
    return int(valor / carregador.preco_kwh * 100 + 1e-9) / 100


def valor_por_kwh(carregador: Carregador, kwh: float) -> float:
    if not carregador.preco_kwh or kwh <= 0:
        return 0.0
    return round(kwh * carregador.preco_kwh, 2)


def medido(sessao: SessaoCarga) -> bool:
    """O carregador informou o medidor no inicio e durante a recarga?"""
    return sessao.meter_inicio_wh is not None and sessao.meter_atual_wh is not None


def kwh_restantes(sessao: SessaoCarga) -> float | None:
    if sessao.kwh_pagos is None:
        return None
    return round(max(0.0, sessao.kwh_pagos - energia_kwh(sessao)), 3)


def atingiu_meta(sessao: SessaoCarga) -> bool:
    if sessao.kwh_pagos is None or not medido(sessao):
        return False
    entregue_wh = sessao.meter_atual_wh - sessao.meter_inicio_wh
    return entregue_wh >= sessao.kwh_pagos * 1000 - settings.KWH_FOLGA_PARADA_WH


def cobrancas_da_sessao(db, sessao: SessaoCarga) -> list[Cobranca]:
    """Pagamento que abriu a recarga + os somados depois, na ordem."""
    return (
        db.query(Cobranca)
        .filter((Cobranca.id == sessao.cobranca_id) | (Cobranca.sessao_id == sessao.id))
        .order_by(Cobranca.id)
        .all()
    )


def calcular_acerto(cobrancas: list[Cobranca], consumido_kwh: float) -> list[tuple[Cobranca, float, float]]:
    """(cobranca, valor consumido, sobra) de cada pagamento que vale."""
    restante = max(0.0, consumido_kwh)
    resultado = []
    for cobranca in cobrancas:
        if cobranca.status != "aprovada" or cobranca.estornada_em is not None or not cobranca.kwh or cobranca.valor <= 0:
            continue
        usado = min(cobranca.kwh, restante)
        restante -= usado
        if usado >= cobranca.kwh - 1e-9:
            consumido = cobranca.valor
        else:
            consumido = min(cobranca.valor, round(usado * cobranca.valor / cobranca.kwh, 2))
        resultado.append((cobranca, consumido, round(cobranca.valor - consumido, 2)))
    return resultado


def acertar(db, sessao: SessaoCarga) -> None:
    """Fim de uma recarga por kWh: registra o consumo e devolve a sobra.
    Roda uma vez por recarga (acerto_em)."""
    from app.services import pagamentos  # pagamentos importa ocpp_handlers

    alterou = (
        db.query(SessaoCarga)
        .filter(SessaoCarga.id == sessao.id, SessaoCarga.acerto_em.is_(None))
        .update({"acerto_em": agora()}, synchronize_session=False)
    )
    db.commit()
    db.refresh(sessao)
    if alterou != 1:
        return  # outra thread ja acertou

    if sessao.motivo_fim == "carro_nao_puxou_energia":
        return  # o valor inteiro ja e' estornado nesse caminho
    carregador = db.get(Carregador, sessao.carregador_id)
    if not medido(sessao):
        if sessao.energia_desde is None:
            return
        sessao.acerto_erro = "O carregador nao informou o medidor: nao da para saber quanto foi usado. Nada foi devolvido."
        registrar_historico(db, sessao.carregador_id, "PAGAMENTO", f"ATENCAO: recarga {sessao.id} por kWh sem leitura do medidor; sobra nao devolvida")
        db.commit()
        return

    consumido_kwh = energia_kwh(sessao)
    devolver = settings.AUTO_ESTORNO and (carregador is None or carregador.devolver_sobra is not False)
    erros = []
    for cobranca, consumido, sobra in calcular_acerto(cobrancas_da_sessao(db, sessao), consumido_kwh):
        cobranca.valor_consumido = consumido
        db.commit()
        if sobra < settings.KWH_DEVOLUCAO_MINIMA or not devolver:
            continue
        if not pagamentos.devolver_parte(db, cobranca, sobra):
            erros.append(f"R$ {sobra:.2f} da cobranca {cobranca.id}")
    if erros:
        sessao.acerto_erro = "O banco recusou devolver " + ", ".join(erros) + ". Devolva pelo painel."
    registrar_historico(
        db,
        sessao.carregador_id,
        "SESSAO",
        f"Recarga {sessao.id} por kWh: usou {consumido_kwh:.2f} de {sessao.kwh_pagos:.2f} kWh pagos",
    )
    db.commit()


def processar(db) -> None:
    """Chamado pelo worker de sessoes: para quem chegou nos kWh pagos e acerta
    as recargas por kWh que terminaram."""
    from app.services import sessoes  # sessoes importa este modulo

    carregando = (
        db.query(SessaoCarga)
        .filter(SessaoCarga.status == "carregando", SessaoCarga.kwh_pagos.isnot(None))
        .all()
    )
    for sessao in carregando:
        if atingiu_meta(sessao):
            registrar_historico(
                db, sessao.carregador_id, "SESSAO", f"Sessao {sessao.id}: entregou {energia_kwh(sessao):.2f} de {sessao.kwh_pagos:.2f} kWh, parando"
            )
            db.commit()
            sessoes.encerrar_sessao(db, sessao, "energia_atingida")

    terminadas = (
        db.query(SessaoCarga)
        .filter(SessaoCarga.status == "finalizada", SessaoCarga.acerto_em.is_(None), SessaoCarga.kwh_pagos.isnot(None))
        .limit(50)
        .all()
    )
    for sessao in terminadas:
        try:
            acertar(db, sessao)
        except Exception as exc:
            db.rollback()
            logging.exception("Erro no acerto da recarga %s", sessao.id)
            # Nao tenta de novo sozinho (poderia devolver duas vezes): fica no painel.
            sessao.acerto_erro = f"Erro no acerto: {exc}"[:500]
            db.commit()
