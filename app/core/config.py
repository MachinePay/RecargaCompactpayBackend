import os

from pydantic import field_validator
from pydantic_settings import BaseSettings


def _normalize_database_url(url: str) -> str:
    """Mesmo ajuste do backend CompactPay: força psycopg2 para Postgres
    (o driver default do SQLAlchemy ja mudou entre versoes)."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


class Settings(BaseSettings):
    APP_VERSION: str = os.getenv("APP_VERSION", "dev")
    # Sem DATABASE_URL roda em SQLite local (bom para bancada). Producao usa Postgres.
    DATABASE_URL: str = "sqlite:///./carregador.db"
    # Chave das rotas /admin (cadastro de carregador, pacotes, terminais). Enquanto
    # o painel nao for integrado ao frontend da CompactPay, e' assim que se administra.
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "")
    # Assinatura do login (JWT). Vazio: derivado da ADMIN_API_KEY.
    JWT_SECRET: str = os.getenv("JWT_SECRET", "")

    # ---- Mercado Pago (cobranca na Point via Orders API, igual
    # /pagamentos/terminal/cobrar da CompactPay) ----
    MP_ACCESS_TOKEN: str = os.getenv("MP_ACCESS_TOKEN", "")
    # MP_SIMULADO=true: nao chama o Mercado Pago. A cobranca fica aguardando e
    # e' aprovada por POST /carregador/cobrancas/{id}/simular-pagamento. Serve
    # para testar o fluxo inteiro na bancada sem maquininha.
    MP_SIMULADO: bool = os.getenv("MP_SIMULADO", "false").lower() == "true"
    # Estorna sozinho quando o cliente pagou e a carga nunca comecou.
    AUTO_ESTORNO: bool = os.getenv("AUTO_ESTORNO", "true").lower() == "true"
    # Cobranca na Point que ninguem pagou expira depois disso.
    COBRANCA_EXPIRA_SEGUNDOS: int = int(os.getenv("COBRANCA_EXPIRA_SEGUNDOS", "180"))

    # ---- Loja e caixa no MP (igual CompactPay: pagamento na maquininha
    # vinculada ao caixa do carregador libera tempo pela regra de preco) ----
    MP_DEFAULT_STORE_STREET_NAME: str = os.getenv("MP_DEFAULT_STORE_STREET_NAME", "Rua CompactPay")
    MP_DEFAULT_STORE_STREET_NUMBER: str = os.getenv("MP_DEFAULT_STORE_STREET_NUMBER", "0")
    MP_DEFAULT_STORE_CITY_NAME: str = os.getenv("MP_DEFAULT_STORE_CITY_NAME", "Sao Paulo")
    MP_DEFAULT_STORE_STATE_NAME: str = os.getenv("MP_DEFAULT_STORE_STATE_NAME", "Sao Paulo")
    MP_DEFAULT_STORE_LATITUDE: float = float(os.getenv("MP_DEFAULT_STORE_LATITUDE", "-23.55052"))
    MP_DEFAULT_STORE_LONGITUDE: float = float(os.getenv("MP_DEFAULT_STORE_LONGITUDE", "-46.633308"))
    MP_DEFAULT_POS_CATEGORY: int = int(os.getenv("MP_DEFAULT_POS_CATEGORY", "7994"))
    MP_POS_CATEGORY_FALLBACKS: str = os.getenv("MP_POS_CATEGORY_FALLBACKS", "7994,7996,7999,5999,5399")

    # ---- OCPP 1.6J ----
    OCPP_HEARTBEAT_INTERVAL: int = int(os.getenv("OCPP_HEARTBEAT_INTERVAL", "60"))
    # Tempo, depois do pagamento, para o carro ser plugado e a carga comecar
    # (StartTransaction). Passou disso: sessao falha e o pagamento e' estornado.
    TEMPO_MAXIMO_INICIO_SEGUNDOS: int = int(os.getenv("TEMPO_MAXIMO_INICIO_SEGUNDOS", "180"))
    # Intervalo entre tentativas de RemoteStopTransaction ate o carregador confirmar.
    STOP_RETRY_SEGUNDOS: int = int(os.getenv("STOP_RETRY_SEGUNDOS", "15"))
    # Tags RFID que liberam carga sem pagamento (manutencao/dono), separadas por virgula.
    OCPP_TAGS_MESTRE: str = os.getenv("OCPP_TAGS_MESTRE", "")

    # Poller do caixa MP: rede de seguranca para webhook que nao chegou
    # (ou nao foi configurado). Le os pagamentos aprovados dos ultimos minutos.
    START_MP_CAIXA_POLLER: bool = os.getenv("START_MP_CAIXA_POLLER", "true").lower() == "true"
    MP_CAIXA_POLLER_INTERVAL_SECONDS: float = float(os.getenv("MP_CAIXA_POLLER_INTERVAL_SECONDS", "15"))
    MP_CAIXA_POLLER_JANELA_MINUTOS: int = int(os.getenv("MP_CAIXA_POLLER_JANELA_MINUTOS", "10"))

    # Poller da SumUp: pagamento digitado na maquininha nao gera webhook.
    START_SUMUP_POLLER_WORKER: bool = os.getenv("START_SUMUP_POLLER_WORKER", "true").lower() == "true"
    SUMUP_POLLER_INTERVAL_SECONDS: float = float(os.getenv("SUMUP_POLLER_INTERVAL_SECONDS", "10"))

    # Avisos de WhatsApp (Evolution API): carregador offline, voltou, resumo.
    START_WHATSAPP_WORKER: bool = os.getenv("START_WHATSAPP_WORKER", "true").lower() == "true"
    WHATSAPP_INTERVALO_SEGUNDOS: float = float(os.getenv("WHATSAPP_INTERVALO_SEGUNDOS", "60"))

    START_SESSOES_WORKER: bool = os.getenv("START_SESSOES_WORKER", "true").lower() == "true"
    SESSOES_WORKER_INTERVAL_SECONDS: float = float(os.getenv("SESSOES_WORKER_INTERVAL_SECONDS", "2"))
    START_COBRANCAS_POLLER_WORKER: bool = os.getenv("START_COBRANCAS_POLLER_WORKER", "true").lower() == "true"
    COBRANCAS_POLLER_INTERVAL_SECONDS: float = float(os.getenv("COBRANCAS_POLLER_INTERVAL_SECONDS", "5"))

    CORS_ALLOWED_ORIGINS: str = os.getenv("CORS_ALLOWED_ORIGINS", "")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _database_url(cls, valor):
        # O BaseSettings le a variavel direto; DATABASE_URL= vazio no .env
        # cairia como "" e quebraria o create_engine. Vazio = SQLite local.
        return _normalize_database_url(valor or "sqlite:///./carregador.db")


settings = Settings()


def tags_mestre() -> set[str]:
    return {t.strip() for t in settings.OCPP_TAGS_MESTRE.split(",") if t.strip()}
