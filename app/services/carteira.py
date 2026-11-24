"""Carteira do motorista (recarga por kWh pelo app).

O que o motorista paga pelo app (Pix ou cartao) vai para a recarga; o saldo
da carteira e' usado primeiro e o banco cobra so a diferenca. No fim da
recarga, o que nao foi usado volta para a carteira (nada volta para o banco:
estorno parcial de cartao nao e' garantido). O saldo so serve para recarregar.

Toda mudanca de saldo grava um MovimentoCarteira com o saldo logo depois, sob
trava (no Postgres, SELECT ... FOR UPDATE na linha do motorista), para duas
abas pagando ao mesmo tempo nao gastarem o mesmo saldo.

Tipos de movimento:
- reserva:    separa o saldo usado numa recarga (sai)
- devolucao:  a recarga nao comecou ou o pagamento nao aconteceu (entra)
- sobra:      o que nao foi usado na recarga (entra)
- ajuste:     feito no painel (entra ou sai)
"""
import threading

from fastapi import HTTPException

from app.db.session import eh_postgres
from app.models.carregador import Cobranca, Motorista, MovimentoCarteira
from app.services.ocpp_handlers import registrar_historico

_trava = threading.Lock()


class SaldoInsuficiente(Exception):
    pass


def saldo(motorista: Motorista | None) -> float:
    return round(motorista.saldo or 0, 2) if motorista is not None else 0.0


def movimentar(db, motorista_id: int, valor: float, tipo: str, descricao: str, cobranca_id: int | None = None) -> float:
    """Soma valor (negativo = tira) ao saldo e grava no extrato. Faz commit.
    Levanta SaldoInsuficiente se o saldo ficaria negativo."""
    valor = round(valor, 2)
    with _trava:
        query = db.query(Motorista).filter(Motorista.id == motorista_id)
        if eh_postgres():
            query = query.with_for_update()
        motorista = query.one()
        db.refresh(motorista)
        novo = round((motorista.saldo or 0) + valor, 2)
        if novo < 0:
            db.rollback()
            raise SaldoInsuficiente()
        motorista.saldo = novo
        db.add(
            MovimentoCarteira(
                motorista_id=motorista_id, tipo=tipo, valor=valor, saldo_apos=novo, descricao=descricao[:300], cobranca_id=cobranca_id
            )
        )
        db.commit()
        return novo


def _ja_tem(db, cobranca_id: int, tipo: str) -> bool:
    return (
        db.query(MovimentoCarteira.id)
        .filter(MovimentoCarteira.cobranca_id == cobranca_id, MovimentoCarteira.tipo == tipo)
        .first()
        is not None
    )


def reservar(db, cobranca: Cobranca) -> None:
    """Tira da carteira o saldo usado nesta recarga (valor_carteira)."""
    if not cobranca.valor_carteira or cobranca.motorista_id is None:
        return
    try:
        movimentar(db, cobranca.motorista_id, -cobranca.valor_carteira, "reserva", f"Recarga #{cobranca.id}: saldo usado", cobranca.id)
    except SaldoInsuficiente:
        raise HTTPException(status_code=409, detail="Seu saldo mudou. Volte e confira o valor de novo.")


def devolver_reserva(db, cobranca: Cobranca) -> None:
    """Pagamento cancelado, recusado ou vencido: o saldo separado volta.
    Idempotente (uma devolucao por reserva)."""
    if not cobranca.valor_carteira or cobranca.motorista_id is None or cobranca.status == "aprovada":
        return
    if not _ja_tem(db, cobranca.id, "reserva") or _ja_tem(db, cobranca.id, "devolucao"):
        return
    movimentar(db, cobranca.motorista_id, cobranca.valor_carteira, "devolucao", f"Recarga #{cobranca.id}: pagamento nao concluido, saldo devolvido", cobranca.id)


def garantir_reserva_ao_aprovar(db, cobranca: Cobranca) -> None:
    """Pagamento aprovado depois de vencido: a reserva ja tinha voltado para a
    carteira. Separa de novo o que der (o resto sai da recarga)."""
    if not cobranca.valor_carteira or cobranca.motorista_id is None or not _ja_tem(db, cobranca.id, "devolucao"):
        return
    motorista = db.get(Motorista, cobranca.motorista_id)
    usar = min(saldo(motorista), cobranca.valor_carteira)
    cobranca.valor_carteira = round(usar, 2)
    db.commit()
    if usar > 0:
        movimentar(db, cobranca.motorista_id, -usar, "reserva", f"Recarga #{cobranca.id}: saldo usado (pagamento confirmado depois)", cobranca.id)


def total_da_recarga(cobranca: Cobranca) -> float:
    """O que foi separado para a recarga: o pago agora + o saldo usado."""
    return round((cobranca.valor or 0) + (cobranca.valor_carteira or 0), 2)


def creditar_sobra(db, cobranca: Cobranca, valor: float) -> None:
    """Fim da recarga: o que nao foi usado volta para a carteira (uma vez)."""
    valor = round(valor, 2)
    if valor <= 0 or cobranca.motorista_id is None or cobranca.valor_creditado:
        return
    novo = movimentar(db, cobranca.motorista_id, valor, "sobra", f"Recarga #{cobranca.id}: energia nao usada voltou", cobranca.id)
    cobranca.valor_creditado = valor
    registrar_historico(db, cobranca.carregador_id, "PAGAMENTO", f"Sobra da cobranca {cobranca.id} na carteira do motorista (saldo {novo:.2f})", valor=valor)
    db.commit()


def devolver_tudo(db, cobranca: Cobranca) -> None:
    """A recarga nao aconteceu (ou o painel estornou): tudo volta para a carteira."""
    total = total_da_recarga(cobranca) - (cobranca.valor_creditado or 0)
    if total <= 0 or cobranca.motorista_id is None:
        return
    movimentar(db, cobranca.motorista_id, total, "devolucao", f"Recarga #{cobranca.id} nao aconteceu: valor devolvido a carteira", cobranca.id)
    cobranca.valor_creditado = round((cobranca.valor_creditado or 0) + total, 2)
    db.commit()


def extrato(db, motorista_id: int, pagina: int = 1, por_pagina: int = 20) -> dict:
    por_pagina = max(1, min(por_pagina, 100))
    pagina = max(1, pagina)
    query = db.query(MovimentoCarteira).filter(MovimentoCarteira.motorista_id == motorista_id)
    total = query.count()
    itens = query.order_by(MovimentoCarteira.id.desc()).offset((pagina - 1) * por_pagina).limit(por_pagina).all()
    return {
        "itens": [
            {"id": m.id, "tipo": m.tipo, "valor": m.valor, "saldo_apos": m.saldo_apos, "descricao": m.descricao, "cobranca_id": m.cobranca_id, "created_at": m.created_at}
            for m in itens
        ],
        "total": total,
        "pagina": pagina,
        "paginas": max(1, -(-total // por_pagina)),
    }
