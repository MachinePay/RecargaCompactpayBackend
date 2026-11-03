"""Registro em memoria de tudo que acontece no OCPP, para a tela de
Diagnostico do painel: tentativas de conexao (inclusive de ID nao
cadastrado), conexoes recusadas e cada mensagem trocada.

Fica em memoria (some quando o servidor reinicia) de proposito: e' para
acompanhar teste ao vivo, nao para auditoria. O historico permanente
continua no HistoricoOperacao.
"""
import itertools
import threading
from collections import deque

from app.models.carregador import agora

MAX_EVENTOS = 1500
MAX_TEXTO = 4000

_eventos: deque = deque(maxlen=MAX_EVENTOS)
_seq = itertools.count(1)
_lock = threading.Lock()


def registrar(
    tipo: str,
    carregador_id: str | None,
    resumo: str,
    *,
    nivel: str = "info",
    acao: str | None = None,
    bruto: str | None = None,
    ip: str | None = None,
) -> None:
    """tipo: tentativa, aceita, recusada, desconectou, recebida, enviada,
    resposta_recebida, resposta_enviada, http.
    nivel: info, ok, aviso, erro."""
    with _lock:
        _eventos.append(
            {
                "seq": next(_seq),
                "momento": agora().isoformat(),
                "tipo": tipo,
                "nivel": nivel,
                "carregador_id": carregador_id,
                "acao": acao,
                "resumo": resumo,
                "bruto": bruto[:MAX_TEXTO] if bruto else None,
                "ip": ip,
            }
        )


def listar(desde: int = 0, carregador_id: str | None = None, limite: int = 300) -> list[dict]:
    with _lock:
        itens = [e for e in _eventos if e["seq"] > desde]
    if carregador_id:
        # Tentativa de ID nao cadastrado aparece em qualquer filtro: e'
        # justamente o que se procura quando o carregador "nao aparece".
        itens = [e for e in itens if e["carregador_id"] == carregador_id or e["tipo"] in ("recusada", "http")]
    return itens[-limite:]


def ultimo_seq() -> int:
    with _lock:
        return _eventos[-1]["seq"] if _eventos else 0


def ids_recusados_nao_cadastrados() -> list[dict]:
    """IDs que tentaram conectar e foram recusados por nao estarem cadastrados."""
    vistos: dict[str, dict] = {}
    with _lock:
        for e in _eventos:
            if e["tipo"] == "recusada" and e["acao"] == "nao_cadastrado" and e["carregador_id"]:
                vistos[e["carregador_id"]] = {"id": e["carregador_id"], "momento": e["momento"], "ip": e["ip"]}
    return list(vistos.values())


def limpar() -> None:
    with _lock:
        _eventos.clear()
