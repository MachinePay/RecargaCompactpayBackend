"""Rotas publicas do app do motorista (sem login).

O motorista le o QR code colado no carregador, escolhe o tempo e paga por Pix.
Fora disso o app mostra o mapa dos carregadores e o historico do celular.

A conta (celular + senha) e' opcional. Sem conta, o navegador gera um id
anonimo (dispositivo) que vai em cada Pix e o historico sai por ele. Com conta
(header Authorization: Bearer), o Pix fica no nome do motorista e o historico
acompanha a pessoa em qualquer celular; ao entrar, as recargas sem conta deste
celular passam para ela.

1. GET  /publico/carregadores                  -> mapa (status e quando libera)
2. GET  /publico/carregadores/{id}             -> tela de escolher o tempo
3. POST /publico/pix                           -> gera o Pix (QR + copia e cola)
4. GET  /publico/cobrancas/{id}?dispositivo=   -> polling ate pagar e da recarga
5. GET  /publico/historico?dispositivo=        -> recargas deste celular (e da conta)
6. POST /publico/conta | /publico/entrar        -> cria a conta / entra (devolve o token)
7. GET  /publico/eu                             -> confere o login
"""
import re
import threading
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cobranca, Motorista, SessaoCarga, agora
from app.services import auth, ocpp_server, pagamentos, pix, sessoes
from app.services.ocpp_handlers import energia_kwh

router = APIRouter(prefix="/publico", tags=["motorista"])

# Conector com carro (carregando ou ainda plugado depois de terminar).
STATUS_COM_CARRO = ("Charging", "SuspendedEV", "SuspendedEVSE", "Finishing")
DISPOSITIVO_VALIDO = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Botoes prontos na tela de escolher o tempo (filtrados pela faixa permitida).
SUGESTOES_MINUTOS = (15, 30, 60, 90, 120, 180)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def motorista_logado(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> Motorista | None:
    """Conta e' opcional: sem token (ou token vencido) segue como anonimo."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    motorista_id = auth.ler_token_motorista(authorization[7:].strip())
    motorista = db.get(Motorista, motorista_id) if motorista_id else None
    return motorista if motorista is not None and motorista.ativo else None


def _dispositivo(valor: str) -> str:
    if not DISPOSITIVO_VALIDO.match(valor or ""):
        raise HTTPException(status_code=422, detail="Dispositivo invalido")
    return valor


def _obter(db: Session, carregador_id: str) -> Carregador:
    carregador = db.get(Carregador, carregador_id)
    if not carregador or not carregador.ativo or carregador.excluido_em is not None:
        raise HTTPException(status_code=404, detail="Carregador nao encontrado")
    return carregador


def _pix_pendente(db: Session, carregador_id: str) -> Cobranca | None:
    return (
        db.query(Cobranca)
        .filter(
            Cobranca.carregador_id == carregador_id,
            Cobranca.status == "aguardando_pagamento",
            Cobranca.origem.in_(("pix", "simulado", "point")),
        )
        .order_by(Cobranca.id.desc())
        .first()
    )


def _situacao(db: Session, carregador: Carregador) -> dict:
    """livre | ocupado | reservado (alguem pagando agora) | offline | defeito,
    com a previsao de quando fica livre."""
    online = ocpp_server.esta_conectado(carregador.id_hardware)
    ativa = sessoes.sessao_ativa(db, carregador.id_hardware)
    segundos = None
    if not online:
        situacao = "offline"
    elif carregador.erro_conector or carregador.status_conector == "Faulted":
        situacao = "defeito"
    elif ativa is not None or carregador.status_conector in STATUS_COM_CARRO:
        situacao = "ocupado"
        if ativa is not None and ativa.status == "carregando":
            segundos = sessoes.segundos_restantes(ativa)
            if segundos is None:
                # O tempo pago so conta quando o carro puxa energia.
                segundos = ativa.minutos_pagos * 60
        elif ativa is not None and ativa.status in ("aguardando_inicio", "aguardando_carro"):
            segundos = ativa.minutos_pagos * 60
        elif ativa is not None:
            segundos = 0  # encerrando
    elif _pix_pendente(db, carregador.id_hardware) is not None:
        situacao = "reservado"
    else:
        situacao = "livre"
    return {
        "situacao": situacao,
        "online": online,
        "status_conector": carregador.status_conector,
        "segundos_para_liberar": segundos,
        "status_recarga": ativa.status if ativa else None,
    }


def _serializar_carregador(db: Session, carregador: Carregador) -> dict:
    preco_hora = pix.valor_por_minutos(carregador, 60) or None
    return {
        "id": carregador.id_hardware,
        "nome": carregador.nome_local,
        "endereco": carregador.endereco,
        "latitude": carregador.latitude,
        "longitude": carregador.longitude,
        "preco_valor": carregador.preco_valor,
        "preco_minutos": carregador.preco_minutos,
        "preco_hora": preco_hora,
        "corrente_maxima_a": carregador.corrente_maxima_a,
        "aceita_pix": bool(preco_hora) and (settings.MP_SIMULADO or bool(pagamentos.token_do_carregador(carregador))),
        **_situacao(db, carregador),
    }


@router.get("/config")
def config():
    """O que o app precisa saber do servidor (chave do mapa do Google)."""
    return {"google_maps_key": settings.GOOGLE_MAPS_KEY or None}


@router.get("/carregadores")
def listar_carregadores(db: Session = Depends(get_db)):
    carregadores = (
        db.query(Carregador)
        .filter(Carregador.ativo.is_(True), Carregador.excluido_em.is_(None))
        .order_by(Carregador.nome_local)
        .all()
    )
    return [_serializar_carregador(db, c) for c in carregadores]


@router.get("/carregadores/{carregador_id}")
def obter_carregador(carregador_id: str, db: Session = Depends(get_db)):
    carregador = _obter(db, carregador_id)
    minimo, maximo = settings.RECARGA_MIN_MINUTOS, settings.RECARGA_MAX_MINUTOS
    return {
        **_serializar_carregador(db, carregador),
        "minutos_min": minimo,
        "minutos_max": maximo,
        "opcoes": [
            {"minutos": m, "valor": pix.valor_por_minutos(carregador, m)}
            for m in SUGESTOES_MINUTOS
            if minimo <= m <= maximo
        ],
    }


class PixCreate(BaseModel):
    carregador_id: str
    minutos: int
    dispositivo: str


def _serializar_cobranca(db: Session, cobranca: Cobranca) -> dict:
    sessao = db.query(SessaoCarga).filter(SessaoCarga.cobranca_id == cobranca.id).first()
    carregador = db.get(Carregador, cobranca.carregador_id)
    return {
        "id": cobranca.id,
        "carregador_id": cobranca.carregador_id,
        "carregador_nome": carregador.nome_local if carregador else cobranca.carregador_id,
        "latitude": carregador.latitude if carregador else None,
        "longitude": carregador.longitude if carregador else None,
        "status": cobranca.status,
        "minutos": cobranca.minutos,
        "valor": cobranca.valor,
        "simulado": cobranca.origem == "simulado",
        "pix_copia_cola": cobranca.pix_copia_cola if cobranca.status == "aguardando_pagamento" else None,
        "segundos_para_vencer": (
            max(0, int((cobranca.pix_expira_em - agora()).total_seconds()))
            if cobranca.pix_expira_em and cobranca.status == "aguardando_pagamento"
            else None
        ),
        "criada_em": cobranca.created_at,
        "pago_em": cobranca.pago_em,
        "estornada": cobranca.estornada_em is not None,
        "sessao": _serializar_sessao(sessao) if sessao else None,
    }


def _serializar_sessao(sessao: SessaoCarga) -> dict:
    return {
        "id": sessao.id,
        "status": sessao.status,
        "minutos_pagos": sessao.minutos_pagos,
        "inicio": sessao.inicio,
        "energia_desde": sessao.energia_desde,
        "fim_previsto": sessao.fim_previsto,
        "fim_real": sessao.fim_real,
        "segundos_restantes": sessoes.segundos_restantes(sessao),
        "energia_kwh": round(energia_kwh(sessao), 3),
        "motivo_fim": sessao.motivo_fim,
    }


def _cobranca_do_dispositivo(db: Session, cobranca_id: int, dispositivo: str, motorista: Motorista | None = None) -> Cobranca:
    cobranca = db.get(Cobranca, cobranca_id)
    da_conta = motorista is not None and cobranca is not None and cobranca.motorista_id == motorista.id
    if not cobranca or (cobranca.dispositivo_id != _dispositivo(dispositivo) and not da_conta):
        raise HTTPException(status_code=404, detail="Recarga nao encontrada")
    return cobranca


@router.post("/pix")
def gerar_pix(dados: PixCreate, db: Session = Depends(get_db), motorista: Motorista | None = Depends(motorista_logado)):
    dispositivo = _dispositivo(dados.dispositivo)
    carregador = _obter(db, dados.carregador_id)
    if not settings.RECARGA_MIN_MINUTOS <= dados.minutos <= settings.RECARGA_MAX_MINUTOS:
        raise HTTPException(
            status_code=422,
            detail=f"Escolha entre {settings.RECARGA_MIN_MINUTOS} e {settings.RECARGA_MAX_MINUTOS} minutos",
        )
    pendente = _pix_pendente(db, carregador.id_hardware)
    if pendente is not None and pendente.dispositivo_id == dispositivo:
        # Mesmo celular gerando de novo (voltou e escolheu outro tempo): troca o Pix.
        pagamentos.cancelar_cobranca(db, pendente)
        db.refresh(pendente)
        if pendente.status == "aguardando_pagamento":
            raise HTTPException(status_code=409, detail="Seu Pix anterior ja esta sendo pago. Aguarde a confirmacao.")
    elif pendente is not None:
        raise HTTPException(status_code=409, detail="Outra pessoa esta pagando neste carregador agora")
    situacao = _situacao(db, carregador)["situacao"]
    if situacao != "livre":
        mensagens = {
            "offline": "O carregador esta sem conexao agora",
            "defeito": "O carregador esta com defeito",
            "ocupado": "O carregador esta em uso",
        }
        raise HTTPException(status_code=409, detail=mensagens.get(situacao, "Carregador indisponivel"))
    cobranca = pix.criar_cobranca_pix(db, carregador, dados.minutos, dispositivo, motorista.id if motorista else None)
    return _serializar_cobranca(db, cobranca)


@router.get("/cobrancas/{cobranca_id}")
def obter_cobranca(
    cobranca_id: int, dispositivo: str = Query(...), db: Session = Depends(get_db), motorista: Motorista | None = Depends(motorista_logado)
):
    return _serializar_cobranca(db, _cobranca_do_dispositivo(db, cobranca_id, dispositivo, motorista))


@router.post("/cobrancas/{cobranca_id}/cancelar")
def cancelar_cobranca(
    cobranca_id: int, dispositivo: str = Query(...), db: Session = Depends(get_db), motorista: Motorista | None = Depends(motorista_logado)
):
    cobranca = _cobranca_do_dispositivo(db, cobranca_id, dispositivo, motorista)
    if not pagamentos.cancelar_cobranca(db, cobranca):
        raise HTTPException(status_code=409, detail="Esse Pix nao pode mais ser cancelado")
    return _serializar_cobranca(db, cobranca)


@router.post("/cobrancas/{cobranca_id}/simular-pagamento")
def simular_pagamento(cobranca_id: int, dispositivo: str = Query(...), db: Session = Depends(get_db)):
    """So com MP_SIMULADO=true: faz o papel do banco pagando o Pix."""
    if not settings.MP_SIMULADO:
        raise HTTPException(status_code=404, detail="Nao disponivel")
    cobranca = _cobranca_do_dispositivo(db, cobranca_id, dispositivo)
    if cobranca.origem != "simulado":
        raise HTTPException(status_code=404, detail="Recarga nao encontrada")
    pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=f"sim-{cobranca.id}", metodo="pix")
    db.refresh(cobranca)
    return _serializar_cobranca(db, cobranca)


@router.get("/historico")
def historico(dispositivo: str = Query(...), db: Session = Depends(get_db), motorista: Motorista | None = Depends(motorista_logado)):
    """Recargas pagas por este celular (e, logado, pela conta), com os totais."""
    filtro = Cobranca.dispositivo_id == _dispositivo(dispositivo)
    if motorista is not None:
        filtro = or_(filtro, Cobranca.motorista_id == motorista.id)
    cobrancas = (
        db.query(Cobranca)
        .filter(filtro, Cobranca.status == "aprovada")
        .order_by(Cobranca.pago_em.desc())
        .limit(200)
        .all()
    )
    itens = [_serializar_cobranca(db, c) for c in cobrancas]
    validas = [i for i in itens if not i["estornada"]]
    em_andamento = [
        i["id"] for i in itens if i["sessao"] and i["sessao"]["status"] in ("aguardando_inicio", "aguardando_carro", "carregando", "encerrando")
    ]
    trinta_dias = agora() - timedelta(days=30)
    return {
        "totais": {
            "recargas": len(validas),
            "valor": round(sum(i["valor"] for i in validas), 2),
            "kwh": round(sum((i["sessao"] or {}).get("energia_kwh") or 0 for i in validas), 3),
            "minutos": sum(i["minutos"] for i in validas),
            "carregadores": len({i["carregador_id"] for i in validas}),
            "recargas_30_dias": sum(1 for i, c in zip(itens, cobrancas) if not i["estornada"] and c.pago_em and c.pago_em >= trinta_dias),
        },
        "em_andamento": em_andamento,
        "recargas": itens,
    }


# ---- Conta do motorista ---------------------------------------------------

MAX_FALHAS_LOGIN = 5
BLOQUEIO_LOGIN = timedelta(minutes=10)
_falhas: dict[str, tuple[int, object]] = {}
_trava_falhas = threading.Lock()


def _telefone(valor: str) -> str:
    """So digitos com DDD; aceita +55 na frente."""
    digitos = re.sub(r"\D", "", valor or "")
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        digitos = digitos[2:]
    if len(digitos) not in (10, 11):
        raise HTTPException(status_code=422, detail="Celular invalido. Use DDD + numero.")
    return digitos


def _serializar_motorista(motorista: Motorista) -> dict:
    return {"id": motorista.id, "nome": motorista.nome, "telefone": motorista.telefone}


def _sessao_da_conta(db: Session, motorista: Motorista, dispositivo: str | None) -> dict:
    """Recargas sem conta deste celular passam para a conta; devolve o token."""
    if dispositivo and DISPOSITIVO_VALIDO.match(dispositivo):
        db.query(Cobranca).filter(Cobranca.dispositivo_id == dispositivo, Cobranca.motorista_id.is_(None)).update(
            {"motorista_id": motorista.id}, synchronize_session=False
        )
    motorista.ultimo_login = agora()
    db.commit()
    return {"token": auth.criar_token_motorista(motorista), "motorista": _serializar_motorista(motorista)}


class ContaCreate(BaseModel):
    nome: str
    telefone: str
    senha: str
    dispositivo: str | None = None


class Entrar(BaseModel):
    telefone: str
    senha: str
    dispositivo: str | None = None


@router.post("/conta")
def criar_conta(dados: ContaCreate, db: Session = Depends(get_db)):
    nome = " ".join((dados.nome or "").split())
    if len(nome) < 2:
        raise HTTPException(status_code=422, detail="Informe seu nome")
    if len(dados.senha or "") < 6:
        raise HTTPException(status_code=422, detail="A senha precisa ter pelo menos 6 caracteres")
    telefone = _telefone(dados.telefone)
    if db.query(Motorista.id).filter(Motorista.telefone == telefone).first():
        raise HTTPException(status_code=409, detail="Ja existe uma conta com esse celular. Entre com a sua senha.")
    motorista = Motorista(nome=nome[:80], telefone=telefone, senha_hash=auth.hash_senha(dados.senha))
    db.add(motorista)
    db.flush()
    return _sessao_da_conta(db, motorista, dados.dispositivo)


@router.post("/entrar")
def entrar(dados: Entrar, db: Session = Depends(get_db)):
    telefone = _telefone(dados.telefone)
    with _trava_falhas:
        falhas, desde = _falhas.get(telefone, (0, None))
        if falhas >= MAX_FALHAS_LOGIN and desde and agora() - desde < BLOQUEIO_LOGIN:
            raise HTTPException(status_code=429, detail="Muitas tentativas. Espere 10 minutos e tente de novo.")
    motorista = db.query(Motorista).filter(Motorista.telefone == telefone).first()
    if motorista is None or not motorista.ativo or not auth.conferir_senha(dados.senha, motorista.senha_hash):
        with _trava_falhas:
            falhas, desde = _falhas.get(telefone, (0, None))
            if desde and agora() - desde >= BLOQUEIO_LOGIN:
                falhas = 0
            _falhas[telefone] = (falhas + 1, agora())
        # Mesma resposta para celular ou senha errados.
        raise HTTPException(status_code=401, detail="Celular ou senha incorretos")
    with _trava_falhas:
        _falhas.pop(telefone, None)
    return _sessao_da_conta(db, motorista, dados.dispositivo)


@router.get("/eu")
def eu(motorista: Motorista | None = Depends(motorista_logado)):
    if motorista is None:
        raise HTTPException(status_code=401, detail="Entre de novo")
    return _serializar_motorista(motorista)
