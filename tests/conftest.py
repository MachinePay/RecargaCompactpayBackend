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
# A trava do conector manda comandos extras; os testes dela ligam de novo.
os.environ.setdefault("BLOQUEAR_CARGA_SEM_PAGAMENTO", "false")
os.environ.setdefault("TEMPO_CONTA_COM_ENERGIA", "false")
os.environ.setdefault("ADMIN_API_KEY", "admin-teste")
os.environ.setdefault("MP_SIMULADO", "true")
os.environ.setdefault("OCPP_TAGS_MESTRE", "RFID-MESTRE")
os.environ.setdefault("APP_VERSION", "test")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest  # noqa: E402

# Credencial de mentira para cliente do Mercado Pago nos testes: o token e'
# "conferido" sem chamar o MP (ver fixture abaixo).
MP_TESTE = {"mp_access_token": "TEST-token-dos-testes", "mp_public_key": "TEST-chave-dos-testes"}


@pytest.fixture(autouse=True)
def _token_mp_de_teste(monkeypatch):
    from app.services import pagamentos

    original = pagamentos.mp_user_id_do_token

    def conferir(token):
        return "999" if token == MP_TESTE["mp_access_token"] else original(token)

    monkeypatch.setattr(pagamentos, "mp_user_id_do_token", conferir)

    # O token de mentira nunca vai a internet: o MP "recusa" na hora (os testes
    # que simulam o MP trocam o mp_request de novo e valem por cima deste).
    from fastapi import HTTPException

    request_original = pagamentos.mp_request

    def mp_request(method, url, token, *args, **kwargs):
        if token == MP_TESTE["mp_access_token"]:
            raise HTTPException(status_code=502, detail="Mercado Pago de teste")
        return request_original(method, url, token, *args, **kwargs)

    monkeypatch.setattr(pagamentos, "mp_request", mp_request)
