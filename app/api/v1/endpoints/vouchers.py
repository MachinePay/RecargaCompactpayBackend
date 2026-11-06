"""Vouchers de recarga gratis.

Painel (admin ou usuario de cliente):
1. GET  /admin/vouchers                 -> lista (cliente ve so os dele)
2. POST /admin/vouchers                 -> cria: minutos, usos, validade e carregadores
3. POST /admin/vouchers/{id}/cancelar   -> o link some na hora

Link publico (sem login), aberto no app do motorista:
4. GET  /publico/voucher/{codigo}        -> minutos e carregadores (404 se venceu/acabou)
5. POST /publico/voucher/{codigo}/usar   -> libera a recarga; acompanha em /app/recarga/{id}

O cliente so escolhe carregadores dele; o admin, qualquer um. Recarga que
termina sem o carro puxar energia devolve o uso ao voucher.
"""
import secrets
import threading
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import not_, or_
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin import Acesso, _busca, _checar_acesso, _paginar, acesso_atual
from app.api.v1.endpoints.motorista import _dispositivo, _serializar_carregador, _serializar_cobranca, get_db, motorista_logado
from app.models.carregador import Carregador, Cobranca, Motorista, SessaoCarga, agora
from app.models.voucher import Voucher, VoucherCarregador, VoucherUso
from app.services import pagamentos
from app.services.ocpp_handlers import registrar_historico

router = APIRouter(tags=["vouchers"])

# Dois celulares usando o ultimo uso ao mesmo tempo: so um leva.
_trava_uso = threading.Lock()


class VoucherCreate(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    minutos: int = Field(ge=1, le=24 * 60)
    usos_max: int = Field(default=1, ge=1, le=1000)
    expira_em: datetime | None = None
    carregadores: list[str] = Field(min_length=1)


def _utc(valor: datetime | None) -> datetime | None:
    if valor is None:
        return None
    return valor.astimezone(timezone.utc).replace(tzinfo=None) if valor.tzinfo else valor


def _usos(db: Session, voucher: Voucher) -> int:
    """Usos que contam: recarga em andamento ou que chegou a dar energia."""
    total = 0
    for uso in db.query(VoucherUso).filter(VoucherUso.voucher_id == voucher.id).all():
        sessao = db.query(SessaoCarga).filter(SessaoCarga.cobranca_id == uso.cobranca_id).first()
        devolvido = (
            sessao is not None
            and sessao.status in ("finalizada", "falhou")
            and sessao.energia_desde is None
            and sessao.motivo_fim != "tempo_esgotado"
        )
        total += 0 if devolvido else 1
    return total


def _carregadores_do_voucher(db: Session, voucher: Voucher) -> list[Carregador]:
    ids = [v.carregador_id for v in db.query(VoucherCarregador).filter(VoucherCarregador.voucher_id == voucher.id).all()]
    return [c for c in (db.get(Carregador, i) for i in ids) if c is not None and c.ativo and c.excluido_em is None]


def _situacao_voucher(db: Session, voucher: Voucher) -> str:
    if not voucher.ativo:
        return "cancelado"
    if voucher.expira_em and agora() >= voucher.expira_em:
        return "vencido"
    if _usos(db, voucher) >= voucher.usos_max:
        return "esgotado"
    return "ativo"


def _serializar(db: Session, voucher: Voucher) -> dict:
    carregadores = db.query(VoucherCarregador).filter(VoucherCarregador.voucher_id == voucher.id).all()
    nomes = {c.id_hardware: c.nome_local for c in db.query(Carregador).filter(Carregador.id_hardware.in_([v.carregador_id for v in carregadores]))}
    return {
        "id": voucher.id,
        "codigo": voucher.codigo,
        "nome": voucher.nome,
        "minutos": voucher.minutos,
        "usos_max": voucher.usos_max,
        "usos": _usos(db, voucher),
        "expira_em": voucher.expira_em,
        "situacao": _situacao_voucher(db, voucher),
        "cliente_id": voucher.cliente_id,
        "criado_por": voucher.criado_por,
        "carregadores": [{"id": v.carregador_id, "nome": nomes.get(v.carregador_id, v.carregador_id)} for v in carregadores],
        "created_at": voucher.created_at,
    }


# ---- Painel -----------------------------------------------------------------


def _voucher_do_painel(db: Session, acesso: Acesso, voucher_id: int) -> Voucher:
    voucher = db.get(Voucher, voucher_id)
    if voucher is None or (not acesso.admin and voucher.cliente_id != acesso.cliente_id):
        raise HTTPException(status_code=404, detail="Voucher nao encontrado")
    return voucher


@router.get("/admin/vouchers")
def listar(
    pagina: int = 1,
    por_pagina: int = 25,
    busca: str | None = None,
    situacao: str | None = None,
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    """Paginada (mais novo primeiro). situacao: validos | vencidos | cancelados.
    Esgotado (todos os usos gastos) aparece em validos, marcado como Usado."""
    query = db.query(Voucher)
    if not acesso.admin:
        query = query.filter(Voucher.cliente_id == acesso.cliente_id)
    query = _busca(query, busca, Voucher.nome, Voucher.codigo)
    momento = agora()
    nao_venceu = or_(Voucher.expira_em.is_(None), Voucher.expira_em > momento)
    if situacao == "validos":
        query = query.filter(Voucher.ativo.is_(True), nao_venceu)
    elif situacao == "vencidos":
        query = query.filter(Voucher.ativo.is_(True), not_(nao_venceu))
    elif situacao == "cancelados":
        query = query.filter(Voucher.ativo.is_(False))
    elif situacao:
        raise HTTPException(status_code=422, detail="Situacao invalida")
    resultado = _paginar(query, pagina, por_pagina, Voucher.id.desc())
    resultado["itens"] = [_serializar(db, v) for v in resultado["itens"]]
    return resultado


@router.post("/admin/vouchers")
def criar(dados: VoucherCreate, db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    # _checar_acesso: cliente so ve (e escolhe) os carregadores dele.
    carregadores = [_checar_acesso(db, acesso, cid) for cid in dict.fromkeys(dados.carregadores)]
    expira_em = _utc(dados.expira_em)
    if expira_em is not None and expira_em <= agora():
        raise HTTPException(status_code=422, detail="A validade precisa ser no futuro")
    donos = {c.cliente_id for c in carregadores}
    voucher = Voucher(
        codigo=secrets.token_urlsafe(8),
        nome=dados.nome.strip(),
        minutos=dados.minutos,
        usos_max=dados.usos_max,
        expira_em=expira_em,
        cliente_id=acesso.cliente_id if not acesso.admin else (donos.pop() if len(donos) == 1 else None),
        criado_por=acesso.nome,
    )
    db.add(voucher)
    db.flush()
    for carregador in carregadores:
        db.add(VoucherCarregador(voucher_id=voucher.id, carregador_id=carregador.id_hardware))
    db.commit()
    return _serializar(db, voucher)


@router.post("/admin/vouchers/{voucher_id}/cancelar")
def cancelar(voucher_id: int, db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    voucher = _voucher_do_painel(db, acesso, voucher_id)
    voucher.ativo = False
    db.commit()
    return _serializar(db, voucher)


# ---- Link publico -------------------------------------------------------------


class UsarVoucher(BaseModel):
    carregador_id: str
    dispositivo: str


def _voucher_publico(db: Session, codigo: str) -> Voucher:
    voucher = db.query(Voucher).filter(Voucher.codigo == codigo).first()
    if voucher is None or _situacao_voucher(db, voucher) != "ativo":
        # Vencido, cancelado ou ja usado: o link deixa de existir.
        raise HTTPException(status_code=404, detail="Voucher nao existe, ja foi usado ou venceu")
    return voucher


@router.get("/publico/voucher/{codigo}")
def ver(codigo: str, db: Session = Depends(get_db)):
    voucher = _voucher_publico(db, codigo)
    return {
        "nome": voucher.nome,
        "minutos": voucher.minutos,
        "usos_restantes": voucher.usos_max - _usos(db, voucher),
        "expira_em": voucher.expira_em,
        "segundos_para_vencer": int((voucher.expira_em - agora()).total_seconds()) if voucher.expira_em else None,
        "carregadores": [_serializar_carregador(db, c) for c in _carregadores_do_voucher(db, voucher)],
    }


@router.post("/publico/voucher/{codigo}/usar")
def usar(codigo: str, dados: UsarVoucher, db: Session = Depends(get_db), motorista: Motorista | None = Depends(motorista_logado)):
    dispositivo = _dispositivo(dados.dispositivo)
    with _trava_uso:
        voucher = _voucher_publico(db, codigo)
        carregador = next((c for c in _carregadores_do_voucher(db, voucher) if c.id_hardware == dados.carregador_id), None)
        if carregador is None:
            raise HTTPException(status_code=404, detail="Este voucher nao vale neste carregador")
        situacao = _serializar_carregador(db, carregador)["situacao"]
        if situacao != "livre":
            mensagens = {
                "offline": "O carregador esta sem conexao agora",
                "defeito": "O carregador esta com defeito",
                "ocupado": "O carregador esta em uso",
                "reservado": "Outra pessoa esta pagando neste carregador agora",
            }
            raise HTTPException(status_code=409, detail=mensagens.get(situacao, "Carregador indisponivel"))
        cobranca = Cobranca(
            carregador_id=carregador.id_hardware,
            minutos=voucher.minutos,
            valor=0,
            origem="voucher",
            metodo="voucher",
            external_reference=f"EV:{carregador.id_hardware}:voucher:{uuid4().hex[:12]}",
            dispositivo_id=dispositivo,
            motorista_id=motorista.id if motorista else None,
        )
        db.add(cobranca)
        db.flush()
        db.add(VoucherUso(voucher_id=voucher.id, cobranca_id=cobranca.id))
        registrar_historico(db, carregador.id_hardware, "PAGAMENTO", f"Voucher \"{voucher.nome}\" usado: {voucher.minutos} min gratis")
        db.commit()
    # Mesmo caminho de um pagamento aprovado: abre a recarga e manda o RemoteStart.
    pagamentos.aprovar_cobranca(db, cobranca.id, payment_id=f"voucher-{voucher.id}", metodo="voucher")
    db.refresh(cobranca)
    return _serializar_cobranca(db, cobranca)
