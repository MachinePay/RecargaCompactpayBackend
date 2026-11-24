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
import statistics
import threading
import time
from datetime import timedelta

from app.core.config import settings
from app.models.carregador import Carregador, Cobranca, SessaoCarga, agora
from app.services.ocpp_handlers import energia_kwh, registrar_historico


# Potencia media por carregador, recalculada no maximo a cada 10 min.
_cache_potencia: dict[str, tuple[float, float | None]] = {}
_cache_lock = threading.Lock()
CACHE_POTENCIA_SEGUNDOS = 600
# Recargas recentes usadas na media.
AMOSTRAS_POTENCIA = 20
# Leitura de potencia ao vivo mais velha que isso nao vale para a previsao.
POTENCIA_AO_VIVO_VALIDA = timedelta(minutes=3)


def potencia_media_kw(db, carregador: Carregador) -> float | None:
    """Quanto este carregador costuma entregar (kW): mediana das ultimas
    recargas medidas (energia / tempo puxando energia). Sem historico, a
    corrente configurada a 220 V. Serve para prever quanto tempo leva."""
    agora_s = time.monotonic()
    with _cache_lock:
        guardado = _cache_potencia.get(carregador.id_hardware)
    if guardado and agora_s - guardado[0] < CACHE_POTENCIA_SEGUNDOS:
        return guardado[1]
    recentes = (
        db.query(SessaoCarga)
        .filter(
            SessaoCarga.carregador_id == carregador.id_hardware,
            SessaoCarga.status == "finalizada",
            SessaoCarga.energia_desde.isnot(None),
            SessaoCarga.fim_real.isnot(None),
            SessaoCarga.meter_inicio_wh.isnot(None),
        )
        .order_by(SessaoCarga.id.desc())
        .limit(AMOSTRAS_POTENCIA)
        .all()
    )
    potencias = []
    for sessao in recentes:
        horas = (sessao.fim_real - sessao.energia_desde).total_seconds() / 3600
        kwh = energia_kwh(sessao)
        if horas >= 5 / 60 and kwh >= 0.2:
            potencias.append(kwh / horas)
    if potencias:
        media = round(statistics.median(potencias), 2)
    elif carregador.corrente_maxima_a:
        media = round(220 * carregador.corrente_maxima_a / 1000, 2)
    else:
        media = None
    with _cache_lock:
        _cache_potencia[carregador.id_hardware] = (agora_s, media)
    return media


def potencia_agora_kw(carregador: Carregador) -> float | None:
    """Potencia que o carro puxa agora (MeterValues), se a leitura e' recente."""
    if not carregador.medido_potencia_w or carregador.medido_potencia_w < 300 or carregador.medido_em is None:
        return None
    if agora() - carregador.medido_em > POTENCIA_AO_VIVO_VALIDA:
        return None
    return carregador.medido_potencia_w / 1000


def previsao_segundos(db, carregador: Carregador, sessao: SessaoCarga) -> int | None:
    """Quanto falta para a recarga terminar, em segundos (aproximado).
    Por tempo: o relogio. Por kWh: o que falta / a potencia de agora (ou a
    media do carregador). Nunca passa do tempo maximo."""
    if sessao.kwh_pagos is None:
        if sessao.status == "carregando" and sessao.fim_previsto is not None:
            return max(0, int((sessao.fim_previsto - agora()).total_seconds()))
        return None
    if sessao.status == "encerrando":
        return 0
    potencia = potencia_agora_kw(carregador) or potencia_media_kw(db, carregador)
    if not potencia:
        return None
    segundos = int((kwh_restantes(sessao) or 0) / potencia * 3600)
    if sessao.fim_previsto is not None:
        segundos = min(segundos, max(0, int((sessao.fim_previsto - agora()).total_seconds())))
    return segundos


def cobra_por_kwh(carregador: Carregador | None) -> bool:
    return carregador is not None and carregador.modo_cobranca == "kwh"


def teto_minutos(carregador: Carregador) -> int:
    return carregador.kwh_tempo_maximo_min or settings.KWH_TEMPO_MAXIMO_MINUTOS


# Pagamento na maquininha que nao e' Pix (cartao): o banco nao devolve parte.
PERCENTUAL_CARTAO_MAQUINA_PADRAO = 95
# Vendas feitas numa maquininha (caixa MP, SumUp, Point do tablet). "simulado"
# e' o teste do painel/bancada.
ORIGENS_MAQUININHA = ("caixa", "sumup", "point", "simulado")


def eh_pix(metodo: str | None) -> bool:
    """Pix no MP vem como payment_type_id "bank_transfer" (ou o id "pix"); na
    SumUp, "pix". O teste da bancada ("simulado") conta como Pix."""
    m = (metodo or "").lower()
    return "pix" in m or m in ("bank_transfer", "simulado")


def percentual_entrega(carregador: Carregador) -> int:
    """% que vira energia no Pix (app e maquininha): o resto e' margem e volta."""
    return carregador.kwh_percentual_entrega or 100


def percentual_cartao_maquina(carregador: Carregador) -> int:
    """% que vira energia no cartao da maquininha (sem devolucao)."""
    return carregador.kwh_percentual_cartao_maquina or PERCENTUAL_CARTAO_MAQUINA_PADRAO


def percentual_da_maquina(carregador: Carregador, metodo: str | None) -> int:
    return percentual_entrega(carregador) if eh_pix(metodo) else percentual_cartao_maquina(carregador)


def devolve_no_banco(cobranca: Cobranca) -> bool:
    """A sobra volta pelo banco? Carteira: nao (volta para a carteira).
    Maquininha: so no Pix (cartao na maquininha nao tem estorno parcial)."""
    if cobranca.carteira:
        return False
    if cobranca.origem in ORIGENS_MAQUININHA:
        return eh_pix(cobranca.metodo)
    return True  # Pix do app antes da carteira


def kwh_por_valor(carregador: Carregador, valor: float, metodo: str | None = "pix") -> float:
    """Energia que um valor livre (maquininha) libera. Pix: o % cadastrado
    (o resto e' margem e volta). Cartao: o % do cartao (padrao 95), sem volta.
    Arredondada para baixo em 0,01 kWh."""
    if not carregador.preco_kwh or carregador.preco_kwh <= 0 or valor <= 0:
        return 0.0
    return int(valor * percentual_da_maquina(carregador, metodo) / 100 / carregador.preco_kwh * 100 + 1e-9) / 100


def valor_por_kwh(carregador: Carregador, kwh: float) -> float:
    """Valor a pagar adiantado para levar kwh (app): o custo da energia mais a
    margem (o inverso de kwh_por_valor). O que nao usar volta no fim."""
    if not carregador.preco_kwh or kwh <= 0:
        return 0.0
    return round(kwh * carregador.preco_kwh * 100 / percentual_entrega(carregador), 2)


def kwh_do_pacote(carregador: Carregador, kwh_pacote: float) -> float:
    """Pacote do tablet: o valor do pacote e' o pre-pago; entrega o percentual."""
    return int(kwh_pacote * percentual_entrega(carregador) + 1e-9) / 100


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
    """(cobranca, valor consumido, sobra) de cada pagamento que vale.

    O usado e' cobrado pelo preco do kWh da venda. Cada pagamento cobre ate o
    valor inteiro em energia (inclusive a margem da maquininha): o que o
    carregador passou do ponto sai da margem antes de ir para o proximo."""
    restante = max(0.0, consumido_kwh)
    resultado = []
    for cobranca in cobrancas:
        # Com carteira, o separado e' o pago agora + o saldo usado.
        total = round((cobranca.valor or 0) + (cobranca.valor_carteira or 0), 2)
        if cobranca.status != "aprovada" or cobranca.estornada_em is not None or not cobranca.kwh or total <= 0:
            continue
        preco = cobranca.preco_kwh or total / cobranca.kwh
        capacidade = total / preco
        usado = min(capacidade, restante)
        restante -= usado
        if usado >= capacidade - 1e-9:
            consumido = total
        else:
            consumido = min(total, round(usado * preco, 2))
        resultado.append((cobranca, consumido, round(total - consumido, 2)))
    return resultado


def acertar(db, sessao: SessaoCarga) -> None:
    """Fim de uma recarga por kWh: registra o consumo e devolve a sobra.
    Roda uma vez por recarga (acerto_em)."""
    from app.services import carteira, pagamentos  # pagamentos importa ocpp_handlers

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
        sessao.acerto_erro = "O carregador nao informou o medidor: nao da para saber quanto foi usado. Nada foi devolvido (nem para a carteira)."
        registrar_historico(db, sessao.carregador_id, "PAGAMENTO", f"ATENCAO: recarga {sessao.id} por kWh sem leitura do medidor; sobra nao devolvida")
        db.commit()
        return

    consumido_kwh = energia_kwh(sessao)
    devolver = settings.AUTO_ESTORNO and (carregador is None or carregador.devolver_sobra is not False)
    erros = []
    for cobranca, consumido, sobra in calcular_acerto(cobrancas_da_sessao(db, sessao), consumido_kwh):
        cobranca.valor_consumido = consumido
        db.commit()
        if cobranca.carteira:
            # App: a sobra (qualquer valor) volta para a carteira do motorista.
            carteira.creditar_sobra(db, cobranca, sobra)
            continue
        if not devolve_no_banco(cobranca):
            continue  # cartao na maquininha: sem devolucao (por isso so 95% vira energia)
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
