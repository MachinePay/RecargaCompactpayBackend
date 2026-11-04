"""Tabelas do sistema do carregador (wallbox OCPP 1.6J).

Mesmo vocabulario da CompactPay: o carregador e' a "maquina" (PK id_hardware,
aqui igual ao ChargePointId que o carregador usa na URL do OCPP), o tablet se
autentica por chave hasheada (igual TerminalSmartApp) e tudo que acontece vai
para historico_operacoes.
"""
import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text

from app.db.base import Base


def agora() -> datetime.datetime:
    """UTC sem fuso (mesmo formato do datetime.utcnow usado na CompactPay)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Carregador(Base):
    __tablename__ = "carregadores"
    # ChargePointId: ultimo pedaco da URL ws://servidor/ocpp/<id_hardware>
    id_hardware = Column(String, primary_key=True)
    nome_local = Column(String, nullable=False)
    connector_id = Column(Integer, default=1, nullable=False)
    # Basic Auth do OCPP (opcional). So o hash fica no banco.
    ocpp_senha_hash = Column(String, nullable=True)
    # Point Smart que cobra por este carregador (terminal_id da Orders API do MP).
    mp_terminal_id = Column(String, nullable=True)
    # Token do dono; vazio usa o MP_ACCESS_TOKEN global.
    mp_access_token = Column(String, nullable=True)
    # Loja e caixa (POS) no MP, criados pelo painel. Pagamento aprovado numa
    # maquininha vinculada a este caixa libera tempo (mercado_pago_caixa.py).
    mp_user_id = Column(String, nullable=True)
    mp_store_id = Column(String, nullable=True)
    mp_store_external_id = Column(String, nullable=True)
    mp_pos_id = Column(String, nullable=True)
    mp_pos_external_id = Column(String, nullable=True)
    # Regra de preco do caixa: preco_valor reais liberam preco_minutos minutos,
    # proporcional (ex.: 10 = 20 min -> R$ 5 da 10 min, R$ 25 da 50 min).
    preco_valor = Column(Float, nullable=True)
    preco_minutos = Column(Integer, nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)

    # Preenchidos pelo proprio carregador (BootNotification/StatusNotification).
    vendor = Column(String, nullable=True)
    modelo = Column(String, nullable=True)
    numero_serie = Column(String, nullable=True)
    firmware_version = Column(String, nullable=True)
    status_conector = Column(String, nullable=True)  # Available, Preparing, Charging, Faulted...
    erro_conector = Column(String, nullable=True)
    ultimo_sinal = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=agora, nullable=False)


class PacoteTempo(Base):
    """Botoes que aparecem no tablet (ex.: R$ 10 = 30 min). Equivalente ao
    Produto da CompactPay."""
    __tablename__ = "pacotes_tempo"
    id = Column(Integer, primary_key=True)
    carregador_id = Column(String, ForeignKey("carregadores.id_hardware"), index=True, nullable=False)
    nome = Column(String, nullable=False)
    minutos = Column(Integer, nullable=False)
    valor = Column(Float, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    ordem = Column(Integer, default=0, nullable=False)


class TerminalTablet(Base):
    """Tablet do totem. Autentica com a chave no header X-Terminal-Key; aqui
    fica so o hash (mesmo esquema do TerminalSmartApp)."""
    __tablename__ = "terminais_tablet"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    chave_prefixo = Column(String, nullable=False)
    chave_hash = Column(String, unique=True, index=True, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=agora, nullable=False)
    ultimo_uso_em = Column(DateTime, nullable=True)


class Cobranca(Base):
    """Uma tentativa de venda: o tablet pede, a Point cobra, o webhook (ou o
    poller) confirma. Status: aguardando_pagamento, aprovada, recusada,
    cancelada, expirada."""
    __tablename__ = "cobrancas"
    id = Column(Integer, primary_key=True)
    carregador_id = Column(String, ForeignKey("carregadores.id_hardware"), index=True, nullable=False)
    pacote_id = Column(Integer, ForeignKey("pacotes_tempo.id"), nullable=True)
    minutos = Column(Integer, nullable=False)
    valor = Column(Float, nullable=False)
    origem = Column(String, default="point", nullable=False)  # point | simulado | teste
    status = Column(String, default="aguardando_pagamento", index=True, nullable=False)
    external_reference = Column(String, unique=True, index=True, nullable=False)
    mp_order_id = Column(String, unique=True, index=True, nullable=True)
    mp_payment_id = Column(String, nullable=True)
    metodo = Column(String, nullable=True)
    detalhe_status = Column(String, nullable=True)
    created_at = Column(DateTime, default=agora, nullable=False)
    pago_em = Column(DateTime, nullable=True)
    estornada_em = Column(DateTime, nullable=True)
    estorno_erro = Column(Text, nullable=True)


class SessaoCarga(Base):
    """Carga liberada por uma cobranca aprovada.

    aguardando_inicio -> RemoteStartTransaction ainda nao aceito (carregador
                         offline ou sem resposta; o worker reenvia)
    aguardando_carro  -> carregador aceitou e espera o carro/StartTransaction
    carregando        -> StartTransaction recebido; o tempo pago esta correndo
    encerrando        -> tempo acabou, RemoteStopTransaction enviado (reenvia
                         ate chegar o StopTransaction)
    finalizada        -> StopTransaction recebido
    falhou            -> nunca comecou; pagamento estornado (se AUTO_ESTORNO)

    O transactionId do OCPP e' o proprio id da sessao: quem gera esse numero e'
    o servidor, na resposta do StartTransaction.
    """
    __tablename__ = "sessoes_carga"
    id = Column(Integer, primary_key=True)
    carregador_id = Column(String, ForeignKey("carregadores.id_hardware"), index=True, nullable=False)
    cobranca_id = Column(Integer, ForeignKey("cobrancas.id"), unique=True, nullable=False)
    connector_id = Column(Integer, default=1, nullable=False)
    # "EV<id>" para sessao paga; tag RFID mestre repete entre sessoes.
    id_tag = Column(String, index=True, nullable=False)
    minutos_pagos = Column(Integer, nullable=False)
    status = Column(String, default="aguardando_inicio", index=True, nullable=False)
    created_at = Column(DateTime, default=agora, nullable=False)
    start_enviado_em = Column(DateTime, nullable=True)
    inicio = Column(DateTime, nullable=True)
    fim_previsto = Column(DateTime, nullable=True)
    fim_real = Column(DateTime, nullable=True)
    motivo_fim = Column(String, nullable=True)
    stop_tentativas = Column(Integer, default=0, nullable=False)
    ultimo_stop_em = Column(DateTime, nullable=True)
    meter_inicio_wh = Column(Integer, nullable=True)
    meter_atual_wh = Column(Integer, nullable=True)
    meter_fim_wh = Column(Integer, nullable=True)


class HistoricoOperacao(Base):
    """Log operacional por carregador, mesmas categorias da CompactPay:
    PAGAMENTO, DISPOSITIVO, SESSAO, TESTE."""
    __tablename__ = "historico_operacoes"
    id = Column(Integer, primary_key=True)
    carregador_id = Column(String, index=True, nullable=False)
    categoria = Column(String, nullable=False)
    descricao = Column(Text, nullable=False)
    valor = Column(Float, nullable=True)
    created_at = Column(DateTime, default=agora, index=True, nullable=False)
