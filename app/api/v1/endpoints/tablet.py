"""Rotas do app do tablet (totem), autenticado pelo header X-Terminal-Key.

Fluxo da tela:
1. GET  /carregador/carregadores/{id}        -> pacotes, online, status do conector
2. POST /carregador/cobrancas                -> manda a cobranca para a Point
3. GET  /carregador/cobrancas/{id}           -> polling ate "aprovada" (traz sessao_id)
4. GET  /carregador/sessoes/{id}             -> polling do tempo restante
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cobranca, PacoteTempo, SessaoCarga, TerminalTablet
from app.services import ocpp_server, pagamentos, sessoes, terminais
from app.services.ocpp_handlers import energia_kwh

router = APIRouter(prefix="/carregador", tags=["tablet"])

# Estados do conector em que da para vender: livre, ou com o cabo ja no carro.
STATUS_CONECTOR_VENDAVEL = ("Available", "Preparing")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_terminal(
    x_terminal_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> TerminalTablet:
    return terminais.autenticar_terminal(db, x_terminal_key)


class CobrancaCreate(BaseModel):
    carregador_id: str
    pacote_id: int


def _obter_carregador(db: Session, carregador_id: str) -> Carregador:
    carregador = db.get(Carregador, carregador_id)
    if not carregador or not carregador.ativo:
        raise HTTPException(status_code=404, detail="Carregador nao encontrado")
    return carregador


def serializar_sessao(sessao: SessaoCarga) -> dict:
    return {
        "id": sessao.id,
        "carregador_id": sessao.carregador_id,
        "status": sessao.status,
        "minutos_pagos": sessao.minutos_pagos,
        "inicio": sessao.inicio,
        "fim_previsto": sessao.fim_previsto,
        "fim_real": sessao.fim_real,
        "segundos_restantes": sessoes.segundos_restantes(sessao),
        "energia_kwh": round(energia_kwh(sessao), 3),
        "motivo_fim": sessao.motivo_fim,
    }


@router.get("/carregadores/{carregador_id}")
def obter_carregador(carregador_id: str, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    carregador = _obter_carregador(db, carregador_id)
    pacotes = (
        db.query(PacoteTempo)
        .filter(PacoteTempo.carregador_id == carregador_id, PacoteTempo.ativo.is_(True))
        .order_by(PacoteTempo.ordem, PacoteTempo.valor)
        .all()
    )
    ativa = sessoes.sessao_ativa(db, carregador_id)
    online = ocpp_server.esta_conectado(carregador_id)
    return {
        "id_hardware": carregador.id_hardware,
        "nome": carregador.nome_local,
        "online": online,
        "status_conector": carregador.status_conector,
        "cabo_conectado": carregador.status_conector in ("Preparing", "SuspendedEV", "SuspendedEVSE", "Charging"),
        "disponivel": online and ativa is None and carregador.status_conector in STATUS_CONECTOR_VENDAVEL,
        "sessao_ativa": serializar_sessao(ativa) if ativa else None,
        "pacotes": [{"id": p.id, "nome": p.nome, "minutos": p.minutos, "valor": p.valor} for p in pacotes],
    }


@router.post("/cobrancas")
def criar_cobranca(dados: CobrancaCreate, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    carregador = _obter_carregador(db, dados.carregador_id)
    pacote = db.get(PacoteTempo, dados.pacote_id)
    if not pacote or not pacote.ativo or pacote.carregador_id != carregador.id_hardware:
        raise HTTPException(status_code=404, detail="Pacote nao encontrado")
    # Nao cobra se nao da pra entregar: offline, ocupado ou com defeito.
    if not ocpp_server.esta_conectado(carregador.id_hardware):
        raise HTTPException(status_code=409, detail="Carregador offline")
    if sessoes.sessao_ativa(db, carregador.id_hardware) is not None:
        raise HTTPException(status_code=409, detail="Carregador em uso")
    if carregador.status_conector not in STATUS_CONECTOR_VENDAVEL:
        raise HTTPException(status_code=409, detail=f"Carregador indisponivel ({carregador.status_conector})")
    pendente = (
        db.query(Cobranca)
        .filter(Cobranca.carregador_id == carregador.id_hardware, Cobranca.status == "aguardando_pagamento")
        .first()
    )
    if pendente is not None:
        raise HTTPException(status_code=409, detail="Ja existe uma cobranca aguardando pagamento")
    cobranca = pagamentos.criar_cobranca(db, carregador, pacote)
    return _serializar_cobranca(db, cobranca)


def _serializar_cobranca(db: Session, cobranca: Cobranca) -> dict:
    sessao = db.query(SessaoCarga).filter(SessaoCarga.cobranca_id == cobranca.id).first()
    return {
        "id": cobranca.id,
        "carregador_id": cobranca.carregador_id,
        "status": cobranca.status,
        "minutos": cobranca.minutos,
        "valor": cobranca.valor,
        "origem": cobranca.origem,
        "estornada": cobranca.estornada_em is not None,
        "sessao": serializar_sessao(sessao) if sessao else None,
    }


@router.get("/cobrancas/{cobranca_id}")
def obter_cobranca(cobranca_id: int, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    cobranca = db.get(Cobranca, cobranca_id)
    if not cobranca:
        raise HTTPException(status_code=404, detail="Cobranca nao encontrada")
    return _serializar_cobranca(db, cobranca)


@router.post("/cobrancas/{cobranca_id}/cancelar")
def cancelar_cobranca(cobranca_id: int, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    cobranca = db.get(Cobranca, cobranca_id)
    if not cobranca:
        raise HTTPException(status_code=404, detail="Cobranca nao encontrada")
    if not pagamentos.cancelar_cobranca(db, cobranca):
        raise HTTPException(status_code=409, detail="Cobranca nao pode mais ser cancelada")
    return _serializar_cobranca(db, cobranca)


@router.post("/cobrancas/{cobranca_id}/simular-pagamento")
def simular_pagamento(cobranca_id: int, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    """So com MP_SIMULADO=true: faz o papel da Point aprovando o pagamento."""
    if not settings.MP_SIMULADO:
        raise HTTPException(status_code=404, detail="Nao disponivel")
    cobranca = db.get(Cobranca, cobranca_id)
    if not cobranca or cobranca.origem != "simulado":
        raise HTTPException(status_code=404, detail="Cobranca nao encontrada")
    pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=f"sim-{cobranca.id}", metodo="simulado")
    db.refresh(cobranca)
    return _serializar_cobranca(db, cobranca)


@router.get("/sessoes/{sessao_id}")
def obter_sessao(sessao_id: int, db: Session = Depends(get_db), terminal=Depends(get_terminal)):
    sessao = db.get(SessaoCarga, sessao_id)
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessao nao encontrada")
    return serializar_sessao(sessao)
