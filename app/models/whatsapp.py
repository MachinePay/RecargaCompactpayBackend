"""Avisos pelo WhatsApp (Evolution API), igual a CompactPay: carregador
offline, lembrete, voltou e resumo diario. Ver app/services/whatsapp_alertas.py."""
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.db.base import Base
from app.models.carregador import agora


class WhatsappConfig(Base):
    """Linha unica (id=1) com a conexao da Evolution API, tempos e textos."""
    __tablename__ = "whatsapp_config"
    id = Column(Integer, primary_key=True)
    ativo = Column(Boolean, default=False, nullable=False)
    evolution_url = Column(String, nullable=True)
    api_key = Column(String, nullable=True)
    instancia = Column(String, nullable=True)
    minutos_offline = Column(Integer, default=30, nullable=False)
    # Lembrete enquanto continua offline: a cada X horas (0 = nunca).
    lembrete_horas = Column(Integer, default=24, nullable=False)
    # Resumo diario: horario padrao (HH:MM, Brasilia); o cliente pode ter outro.
    resumo_horario = Column(String, default="22:00", nullable=False)
    mensagem_offline = Column(Text, nullable=True)
    mensagem_lembrete = Column(Text, nullable=True)
    mensagem_voltou = Column(Text, nullable=True)
    mensagem_resumo = Column(Text, nullable=True)
    # Quando foi ativado: carregador que ja estava offline antes nao dispara
    # (evita uma rajada de avisos antigos ao ligar o envio).
    ativado_em = Column(DateTime, nullable=True)
    # Diagnostico do ultimo ciclo (mostrado na tela).
    ultimo_ciclo_em = Column(DateTime, nullable=True)
    ultimo_ciclo_status = Column(String, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WhatsappCliente(Base):
    """O que cada cliente recebe. Sem linha, nao recebe nada. O numero e' o
    telefone do cadastro do cliente."""
    __tablename__ = "whatsapp_clientes"
    cliente_id = Column(Integer, ForeignKey("clientes.id"), primary_key=True)
    ativo = Column(Boolean, default=False, nullable=False)  # aviso de offline
    recebe_lembrete = Column(Boolean, default=False, nullable=False)
    recebe_voltou = Column(Boolean, default=False, nullable=False)
    resumo_diario = Column(Boolean, default=False, nullable=False)
    resumo_horario = Column(String, nullable=True)  # HH:MM; vazio = padrao
    resumo_ultimo_dia = Column(String, nullable=True)  # AAAA-MM-DD (Brasilia)
    ativado_em = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class WhatsappOfflineAviso(Base):
    """Uma queda avisada (carregador + ultimo_sinal). Controla o lembrete e o
    "voltou" e impede avisar a mesma queda duas vezes."""
    __tablename__ = "whatsapp_offline_avisos"
    __table_args__ = (UniqueConstraint("carregador_id", "offline_desde", name="uq_whatsapp_aviso_queda"),)
    id = Column(Integer, primary_key=True)
    carregador_id = Column(String, index=True, nullable=False)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    offline_desde = Column(DateTime, nullable=False)
    avisado_em = Column(DateTime, nullable=False)
    ultimo_lembrete_em = Column(DateTime, nullable=True)
    lembretes = Column(Integer, default=0, nullable=False)
    resolvido_em = Column(DateTime, nullable=True)


class WhatsappEnvio(Base):
    """Historico de cada mensagem enviada (ou que falhou)."""
    __tablename__ = "whatsapp_envios"
    id = Column(Integer, primary_key=True)
    cliente_id = Column(Integer, ForeignKey("clientes.id"), index=True, nullable=True)
    carregador_ids = Column(String, nullable=True)  # separados por virgula
    tipo = Column(String, nullable=False)  # offline | lembrete | voltou | resumo | teste
    telefone = Column(String, nullable=False)
    mensagem = Column(Text, nullable=False)
    status = Column(String, nullable=False)  # enviado | erro
    erro = Column(String, nullable=True)
    created_at = Column(DateTime, default=agora, nullable=False)
