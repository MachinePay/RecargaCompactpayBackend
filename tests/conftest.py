import os
import sys
import tempfile
from pathlib import Path

# Antes de qualquer import de app.*: Settings() le estes valores uma vez so.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/carregador-test.db")
os.environ.setdefault("START_SESSOES_WORKER", "false")
os.environ.setdefault("START_COBRANCAS_POLLER_WORKER", "false")
os.environ.setdefault("START_SUMUP_POLLER_WORKER", "false")
os.environ.setdefault("START_WHATSAPP_WORKER", "false")
os.environ.setdefault("START_MP_CAIXA_POLLER", "false")
os.environ.setdefault("ADMIN_API_KEY", "admin-teste")
os.environ.setdefault("MP_SIMULADO", "true")
os.environ.setdefault("OCPP_TAGS_MESTRE", "RFID-MESTRE")
os.environ.setdefault("APP_VERSION", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
