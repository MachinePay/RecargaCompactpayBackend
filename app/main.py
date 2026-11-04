import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.endpoints import ocpp
from app.api.v1.routes import api_router
from app.core.config import settings
from app.db.base import Base
from app.db.migracoes import adicionar_colunas_faltando
from app.db.session import engine
import app.models.carregador  # noqa: F401 - registra as tabelas para o create_all
from app.services import diagnostico
from app.services.pagamentos import start_cobrancas_poller
from app.services.sessoes import start_sessoes_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Mesmo esquema da CompactPay: sem Alembic, create_all no startup.
    Base.metadata.create_all(bind=engine)
    adicionar_colunas_faltando(engine, Base)
    if not settings.ADMIN_API_KEY:
        logging.warning("ADMIN_API_KEY vazio: rotas /admin ficam bloqueadas")
    start_sessoes_worker()
    start_cobrancas_poller()
    yield


app = FastAPI(title="Carregador EV - CompactPay", version=settings.APP_VERSION, lifespan=lifespan)

origens = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
if origens:
    app.add_middleware(CORSMiddleware, allow_origins=origens, allow_methods=["*"], allow_headers=["*"])



@app.middleware("http")
async def registrar_http_desconhecido(request: Request, call_next):
    # Carregador mal configurado as vezes bate por HTTP comum (sem WebSocket)
    # num caminho qualquer. Fica registrado no Diagnostico para aparecer.
    resposta = await call_next(request)
    if resposta.status_code == 404 and not request.url.path.startswith("/api/"):
        diagnostico.registrar(
            "http",
            None,
            f"Pedido HTTP {request.method} em {request.url.path} (sem WebSocket)",
            nivel="aviso",
            ip=ocpp.ip_cliente(request),
        )
    return resposta


app.include_router(api_router, prefix="/api/v1")
app.include_router(ocpp.router)


@app.get("/")
def raiz():
    return {"ok": True, "versao": settings.APP_VERSION}
