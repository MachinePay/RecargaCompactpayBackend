"""Tabelas do sistema do carregador (wallbox OCPP 1.6J).

Mesmo vocabulario da CompactPay: o carregador e' a "maquina" (PK id_hardware,
aqui igual ao ChargePointId que o carregador usa na URL do OCPP), o tablet se
autentica por chave hasheada (igual TerminalSmartApp) e tudo que acontece vai
para historico_operacoes.
"""
import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.base import Base


def agora() -> datetime.datetime:
    """UTC sem fuso (mesmo formato do datetime.utcnow usado na CompactPay)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Cliente(Base):
    """Dono dos carregadores, igual ao Cliente da CompactPay: cada um tem a
    propria conta do Mercado Pago (access token) e o endereco usado na loja
    do MP. O token nunca sai do servidor."""
    __tablename__ = "clientes"
    id = Column(Integer, primary_key=True)
    nome_empresa = Column(String, nullable=False)
    email_contato = Column(String, nullable=True)
    telefone = Column(String, nullable=True)
    documento = Column(String, nullable=True)  # CPF ou CNPJ
    endereco_rua = Column(String, nullable=True)
    endereco_numero = Column(String, nullable=True)
    endereco_cidade = Column(String, nullable=True)
    endereco_estado = Column(String, nullable=True)
    endereco_latitude = Column(Float, nullable=True)
    endereco_longitude = Column(Float, nullable=True)
    mp_access_token = Column(String, nullable=True)
    # Guardados para um dia usar (igual Cliente da CompactPay): Public Key para
    # checkout de cartao; Client ID/Secret para o "Conectar Mercado Pago" (OAuth).
    mp_public_key = Column(String, nullable=True)
    mp_client_id = Column(String, nullable=True)
    mp_client_secret = Column(String, nullable=True)
    # Conta do MP dona do token (GET /users/me). O webhook manda esse user_id,
    # e e' por ele que se acha o token certo (resolve_mp_token_by_user_id).
    mp_user_id = Column(String, nullable=True, index=True)
    # Categoria (MCC) que o MP aceitou no ultimo caixa criado.
    mp_pos_category = Column(Integer, nullable=True)
    # Bancos que o cliente usa (igual cliente_mercado_pago/cliente_sumup da CompactPay).
    cliente_mercado_pago = Column(Boolean, nullable=True)
    cliente_sumup = Column(Boolean, nullable=True)
    sumup_api_key = Column(String, nullable=True)
    sumup_merchant_code = Column(String, nullable=True)
    # Ate onde o poller ja leu o historico de transacoes da SumUp.
    sumup_last_sync_at = Column(DateTime, nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=agora, nullable=False)


class Usuario(Base):
    """Login do painel (igual Usuarios da CompactPay). role admin ve tudo;
    role cliente ve so os carregadores do cliente_id dele."""
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    senha_hash = Column(String, nullable=False)
    role = Column(String, default="cliente", nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    cliente = relationship(Cliente)
    ativo = Column(Boolean, default=True, nullable=False)
    ultimo_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=agora, nullable=False)


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
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    cliente = relationship(Cliente)
    # Banco da maquininha deste carregador: mercado_pago (loja/caixa) ou sumup.
    # Vazio (carregador antigo) conta como mercado_pago.
    banco_pagamento = Column(String, nullable=True)
    # SumUp: reader pareado (id na API), serial da maquininha (card_reader.code
    # do recibo) e operador logado nela (identifica o Pix, que vem sem serial).
    sumup_reader_id = Column(String, nullable=True)
    sumup_device_code = Column(String, nullable=True)
    sumup_usuario = Column(String, nullable=True)
    # Token so deste carregador (legado). O normal e' o token do cliente.
    mp_access_token = Column(String, nullable=True)
    # Loja e caixa (POS) no MP, criados pelo painel. Pagamento aprovado numa
    # maquininha vinculada a este caixa libera tempo (mercado_pago_caixa.py).
    mp_user_id = Column(String, nullable=True)
    mp_store_id = Column(String, nullable=True)
    mp_store_external_id = Column(String, nullable=True)
    mp_pos_id = Column(String, nullable=True)
    mp_pos_external_id = Column(String, nullable=True)
    # Onde o carregador esta (mapa do painel). O CEP e' opcional e serve
    # para preencher endereco e coordenadas aproximadas (services/geo.py).
    cep = Column(String, nullable=True)
    endereco = Column(String, nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    # Regra de preco do caixa: preco_valor reais liberam preco_minutos minutos,
    # proporcional (ex.: 10 = 20 min -> R$ 5 da 10 min, R$ 25 da 50 min).
    preco_valor = Column(Float, nullable=True)
    preco_minutos = Column(Integer, nullable=True)
    ativo = Column(Boolean, default=True, nullable=False)
    # Excluido pelo painel mas com vendas no historico: some das telas e recusa
    # a conexao, e as vendas antigas continuam nos relatorios.
    excluido_em = Column(DateTime, nullable=True)
    # Corrente maxima (A) definida no painel; o servidor manda ao carregador
    # (SetChargingProfile) e repete quando ele reconecta. Vazio = a do aparelho.
    corrente_maxima_a = Column(Integer, nullable=True)

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
    # Quando o carro comecou a puxar energia: e' dai que o tempo pago conta
    # (com TEMPO_CONTA_COM_ENERGIA). Vazio = carro ainda nao puxou.
    energia_desde = Column(DateTime, nullable=True)
    fim_previsto = Column(DateTime, nullable=True)
    fim_real = Column(DateTime, nullable=True)
    motivo_fim = Column(String, nullable=True)
    stop_tentativas = Column(Integer, default=0, nullable=False)
    # Pagou antes de plugar: o JST-901 encerra a transacao (sem carro) quando o
    # carro e' plugado. A recarga volta a aguardando_inicio e liga de novo.
    reinicios = Column(Integer, nullable=True)
    reiniciado_em = Column(DateTime, nullable=True)
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
