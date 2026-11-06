"""Voucher de recarga gratis: o admin (qualquer carregador) ou o cliente (so os
dele) gera um link com X minutos, usavel em um dos carregadores escolhidos,
ate vencer. Ver app/api/v1/endpoints/vouchers.py."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String

from app.db.base import Base
from app.models.carregador import agora


class Voucher(Base):
    __tablename__ = "vouchers"
    id = Column(Integer, primary_key=True)
    # Vai no link (/v/{codigo}); aleatorio para ninguem adivinhar.
    codigo = Column(String, unique=True, index=True, nullable=False)
    nome = Column(String, nullable=False)  # ex.: "Cortesia Joao"
    minutos = Column(Integer, nullable=False)
    usos_max = Column(Integer, default=1, nullable=False)
    # Vazio = nao vence. Vencido, o link some (404).
    expira_em = Column(DateTime, nullable=True)
    # Dono: cliente que criou (ou o dos carregadores, quando o admin cria).
    # Vazio = do admin, com carregadores de mais de um cliente.
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    criado_por = Column(String, nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=agora, nullable=False)


class VoucherCarregador(Base):
    """Carregadores em que o voucher pode ser usado."""
    __tablename__ = "voucher_carregadores"
    id = Column(Integer, primary_key=True)
    voucher_id = Column(Integer, ForeignKey("vouchers.id"), index=True, nullable=False)
    carregador_id = Column(String, ForeignKey("carregadores.id_hardware"), nullable=False)


class VoucherUso(Base):
    """Cada uso vira uma cobranca de R$ 0 (origem "voucher") ja aprovada, que
    abre a recarga pelo mesmo caminho do Pix."""
    __tablename__ = "voucher_usos"
    id = Column(Integer, primary_key=True)
    voucher_id = Column(Integer, ForeignKey("vouchers.id"), index=True, nullable=False)
    cobranca_id = Column(Integer, ForeignKey("cobrancas.id"), nullable=False)
    created_at = Column(DateTime, default=agora, nullable=False)
