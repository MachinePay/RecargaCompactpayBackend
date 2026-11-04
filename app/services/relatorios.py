"""Relatorio de gestao por periodo (tela Gestao do painel): faturamento,
graficos por dia e por hora, ranking de carregadores e formas de pagamento.

Datas e horas no horario de Brasilia (UTC-3, sem horario de verao); o banco
guarda UTC sem fuso.
"""
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.models.carregador import Carregador, Cobranca, HistoricoOperacao, SessaoCarga, agora
from app.services.ocpp_handlers import energia_kwh

FUSO = timedelta(hours=3)
PERIODOS = ("hoje", "7d", "30d", "mes", "mes_anterior", "personalizado")
ORIGENS_VENDA = ("caixa", "point", "sumup")


def _hoje_br() -> date:
    return (agora() - FUSO).date()


def intervalo(periodo: str, inicio: date | None, fim: date | None) -> tuple[date, date]:
    """Dias de Brasilia, fim incluso."""
    hoje = _hoje_br()
    if periodo == "hoje":
        return hoje, hoje
    if periodo == "7d":
        return hoje - timedelta(days=6), hoje
    if periodo == "30d":
        return hoje - timedelta(days=29), hoje
    if periodo == "mes":
        return hoje.replace(day=1), hoje
    if periodo == "mes_anterior":
        fim_anterior = hoje.replace(day=1) - timedelta(days=1)
        return fim_anterior.replace(day=1), fim_anterior
    if periodo == "personalizado" and inicio and fim and inicio <= fim and (fim - inicio).days <= 366:
        return inicio, fim
    raise HTTPException(status_code=422, detail="Periodo invalido")


def _utc(dia: date) -> datetime:
    """Meia-noite de Brasilia do dia, em UTC sem fuso."""
    return datetime(dia.year, dia.month, dia.day) + FUSO


def _vendas(db, de: datetime, ate: datetime, ids: list[str], incluir_simulados: bool) -> list[Cobranca]:
    origens = ORIGENS_VENDA + (("simulado",) if incluir_simulados else ())
    return (
        db.query(Cobranca)
        .filter(
            Cobranca.status == "aprovada",
            Cobranca.pago_em >= de,
            Cobranca.pago_em < ate,
            Cobranca.origem.in_(origens),
            Cobranca.carregador_id.in_(ids),
        )
        .all()
    )


def _sessoes(db, de: datetime, ate: datetime, ids: list[str]) -> list[SessaoCarga]:
    return db.query(SessaoCarga).filter(SessaoCarga.inicio >= de, SessaoCarga.inicio < ate, SessaoCarga.carregador_id.in_(ids)).all()


def _somar(vendas: list[Cobranca], sessoes: list[SessaoCarga]) -> dict:
    validas = [v for v in vendas if v.estornada_em is None]
    faturamento = round(sum(v.valor for v in validas), 2)
    return {
        "faturamento": faturamento,
        "vendas": len(validas),
        "ticket_medio": round(faturamento / len(validas), 2) if validas else 0,
        "recargas": len(sessoes),
        "energia_kwh": round(sum(energia_kwh(s) for s in sessoes), 3),
        "minutos_vendidos": sum(v.minutos for v in validas),
        "estornos_qtd": len(vendas) - len(validas),
        "estornos_valor": round(sum(v.valor for v in vendas if v.estornada_em is not None), 2),
    }


def gerar(db, periodo: str, inicio: date | None, fim: date | None, cliente_id: int | None, carregador_id: str | None, incluir_simulados: bool) -> dict:
    dia_ini, dia_fim = intervalo(periodo, inicio, fim)
    de, ate = _utc(dia_ini), _utc(dia_fim + timedelta(days=1))
    dias = (dia_fim - dia_ini).days + 1

    query = db.query(Carregador)
    if cliente_id:
        query = query.filter(Carregador.cliente_id == cliente_id)
    if carregador_id:
        query = query.filter(Carregador.id_hardware == carregador_id)
    carregadores = query.all()
    ids = [c.id_hardware for c in carregadores]

    vendas = _vendas(db, de, ate, ids, incluir_simulados)
    sessoes = _sessoes(db, de, ate, ids)
    # Periodo anterior do mesmo tamanho, para a comparacao dos cartoes.
    anterior_de = de - timedelta(days=dias)
    anterior = _somar(_vendas(db, anterior_de, de, ids, incluir_simulados), _sessoes(db, anterior_de, de, ids))

    por_dia = {dia_ini + timedelta(days=i): {"faturamento": 0.0, "vendas": 0, "energia_kwh": 0.0} for i in range(dias)}
    # grade[dia_da_semana][hora], 0 = segunda (igual HorariosPico da CompactPay).
    grade = [[{"faturamento": 0.0, "vendas": 0} for _ in range(24)] for _ in range(7)]
    por_metodo: dict[str, dict] = {}
    for venda in vendas:
        if venda.estornada_em is not None:
            continue
        local = venda.pago_em - FUSO
        dia = por_dia[local.date()]
        dia["faturamento"] = round(dia["faturamento"] + venda.valor, 2)
        dia["vendas"] += 1
        celula = grade[local.weekday()][local.hour]
        celula["faturamento"] = round(celula["faturamento"] + venda.valor, 2)
        celula["vendas"] += 1
        metodo = por_metodo.setdefault(venda.metodo or venda.origem, {"metodo": venda.metodo or venda.origem, "valor": 0.0, "vendas": 0})
        metodo["valor"] = round(metodo["valor"] + venda.valor, 2)
        metodo["vendas"] += 1
    for sessao in sessoes:
        dia = por_dia[(sessao.inicio - FUSO).date()]
        dia["energia_kwh"] = round(dia["energia_kwh"] + energia_kwh(sessao), 3)

    quedas: dict[str, int] = {}
    for h in (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.created_at >= de,
            HistoricoOperacao.created_at < ate,
            HistoricoOperacao.carregador_id.in_(ids),
            HistoricoOperacao.descricao == "Carregador desconectou do servidor OCPP",
        )
        .all()
    ):
        quedas[h.carregador_id] = quedas.get(h.carregador_id, 0) + 1

    minutos_periodo = (min(ate, agora()) - de).total_seconds() / 60
    ranking = []
    for carregador in carregadores:
        meus = [s for s in sessoes if s.carregador_id == carregador.id_hardware]
        # Ocupacao: tempo com recarga aberta sobre o tempo do periodo (ate agora).
        ocupado = sum(((s.fim_real or min(ate, agora())) - s.inicio).total_seconds() / 60 for s in meus)
        ranking.append(
            {
                "id_hardware": carregador.id_hardware,
                "nome_local": carregador.nome_local,
                "cliente_nome": carregador.cliente.nome_empresa if carregador.cliente else None,
                **_somar([v for v in vendas if v.carregador_id == carregador.id_hardware], meus),
                "ocupacao_pct": round(min(100.0, 100 * ocupado / minutos_periodo), 1) if minutos_periodo > 0 else 0,
                "quedas": quedas.get(carregador.id_hardware, 0),
            }
        )
    ranking.sort(key=lambda r: (-r["faturamento"], -r["recargas"], r["nome_local"]))

    return {
        "periodo": {"inicio": dia_ini.isoformat(), "fim": dia_fim.isoformat(), "dias": dias},
        "totais": _somar(vendas, sessoes),
        "anterior": anterior,
        "por_dia": [{"dia": d.isoformat(), **v} for d, v in por_dia.items()],
        "grade": grade,
        "ranking": ranking,
        "por_metodo": sorted(por_metodo.values(), key=lambda m: -m["valor"]),
    }
