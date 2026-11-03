"""Central System OCPP 1.6J: guarda as conexoes WebSocket abertas e faz o
enquadramento das mensagens. Regra de negocio fica em ocpp_handlers.py.

No OCPP o carregador e' o CLIENTE: ele abre o WebSocket para
ws(s)://servidor/ocpp/<ChargePointId> e fica conectado. Toda mensagem e' um
array JSON:
    [2, id, acao, payload]   chamada  (CALL)
    [3, id, payload]         resposta (CALLRESULT)
    [4, id, codigo, descricao, detalhes]  erro (CALLERROR)

As conexoes ficam em memoria, entao o servidor tem que rodar com UMA
instancia so (um processo uvicorn, sem --workers). Com mais de uma, o worker
de uma instancia nao enxerga o carregador conectado na outra.

Banco e' sincrono (SQLAlchemy igual CompactPay): todo handler roda em
threadpool para nao travar o loop do WebSocket. enviar_chamada() e' chamada
de threads (endpoints/workers), nunca de dentro do loop.
"""
import asyncio
import concurrent.futures
import json
import logging
import threading
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.services import diagnostico, ocpp_handlers

CALL = 2
CALLRESULT = 3
CALLERROR = 4
ENVIO_TIMEOUT_SECONDS = 5


def _json(mensagem) -> str:
    """JSON compacto, sem espacos. O firmware do JST-901 (WGD ICS_10) nao
    entende `"status": "Accepted"` com espaco: repetia o BootNotification a
    cada 10 s e ignorava nossos comandos. Ele mesmo manda sem espacos."""
    return json.dumps(mensagem, separators=(",", ":"), ensure_ascii=False)


class ErroOcpp(Exception):
    """Resposta CALLERROR do carregador para uma chamada nossa."""

    def __init__(self, codigo: str, descricao: str = ""):
        super().__init__(f"{codigo}: {descricao}")
        self.codigo = codigo
        self.descricao = descricao


class ConexaoCarregador:
    def __init__(self, charge_point_id: str, websocket: WebSocket, loop: asyncio.AbstractEventLoop):
        self.charge_point_id = charge_point_id
        self.websocket = websocket
        self.loop = loop
        self.pendentes: dict[str, tuple[str, concurrent.futures.Future]] = {}


_conexoes: dict[str, ConexaoCarregador] = {}
_lock = threading.Lock()


def esta_conectado(charge_point_id: str) -> bool:
    with _lock:
        return charge_point_id in _conexoes


def conectados() -> list[str]:
    with _lock:
        return sorted(_conexoes)


def enviar_chamada(charge_point_id: str, acao: str, payload: dict) -> concurrent.futures.Future | None:
    """Manda uma chamada para o carregador. Devolve um Future que recebe o
    payload da resposta (ou ErroOcpp), ou None se o carregador nao esta
    conectado. Nao espera a resposta: quem precisa usa futuro.result(timeout)
    ou futuro.add_done_callback (o callback roda fora do loop)."""
    with _lock:
        conexao = _conexoes.get(charge_point_id)
    if conexao is None:
        return None
    try:
        loop_atual = asyncio.get_running_loop()
    except RuntimeError:
        loop_atual = None
    if loop_atual is conexao.loop:
        # envio.result() abaixo travaria o proprio loop.
        raise RuntimeError("enviar_chamada nao pode ser chamada de dentro do loop do WebSocket")

    msg_id = uuid4().hex
    futuro: concurrent.futures.Future = concurrent.futures.Future()
    conexao.pendentes[msg_id] = (acao, futuro)
    texto = _json([CALL, msg_id, acao, payload])
    logging.info("[OCPP %s] >> %s", charge_point_id, texto)
    diagnostico.registrar("enviada", charge_point_id, f"Servidor pediu {acao}", acao=acao, bruto=texto)
    envio = asyncio.run_coroutine_threadsafe(conexao.websocket.send_text(texto), conexao.loop)
    try:
        envio.result(timeout=ENVIO_TIMEOUT_SECONDS)
    except Exception:
        conexao.pendentes.pop(msg_id, None)
        logging.exception("[OCPP %s] falha ao enviar %s", charge_point_id, acao)
        diagnostico.registrar("enviada", charge_point_id, f"Falha ao enviar {acao}", nivel="erro", acao=acao)
        return None
    return futuro


async def atender_conexao(websocket: WebSocket, charge_point_id: str) -> None:
    """Loop de uma conexao ja aceita, ate o carregador desconectar."""
    conexao = ConexaoCarregador(charge_point_id, websocket, asyncio.get_running_loop())
    with _lock:
        antiga = _conexoes.get(charge_point_id)
        _conexoes[charge_point_id] = conexao
    if antiga is not None:
        # Carregador reconectou sem fechar a anterior (queda de Wi-Fi): fica a nova.
        logging.info("[OCPP %s] nova conexao substitui a anterior", charge_point_id)
        _falhar_pendentes(antiga, "conexao substituida")
        try:
            await antiga.websocket.close()
        except Exception:
            pass

    await run_in_threadpool(ocpp_handlers.ao_conectar, charge_point_id)
    try:
        while True:
            texto = await websocket.receive_text()
            await _processar_mensagem(conexao, texto)
    except WebSocketDisconnect:
        pass
    finally:
        removida = False
        with _lock:
            if _conexoes.get(charge_point_id) is conexao:
                del _conexoes[charge_point_id]
                removida = True
        _falhar_pendentes(conexao, "carregador desconectou")
        if removida:
            await run_in_threadpool(ocpp_handlers.ao_desconectar, charge_point_id)


def _falhar_pendentes(conexao: ConexaoCarregador, motivo: str) -> None:
    for acao, futuro in list(conexao.pendentes.values()):
        if not futuro.done():
            futuro.set_exception(ErroOcpp("Desconectado", f"{acao}: {motivo}"))
    conexao.pendentes.clear()


async def _processar_mensagem(conexao: ConexaoCarregador, texto: str) -> None:
    cp_id = conexao.charge_point_id
    logging.info("[OCPP %s] << %s", cp_id, texto)
    try:
        mensagem = json.loads(texto)
        tipo = mensagem[0]
        msg_id = str(mensagem[1])
    except (ValueError, IndexError, TypeError):
        logging.warning("[OCPP %s] mensagem invalida ignorada", cp_id)
        diagnostico.registrar("recebida", cp_id, "Mensagem fora do formato OCPP (ignorada)", nivel="aviso", bruto=texto)
        return

    if tipo == CALL:
        acao = mensagem[2] if len(mensagem) > 2 else ""
        payload = mensagem[3] if len(mensagem) > 3 and isinstance(mensagem[3], dict) else {}
        diagnostico.registrar("recebida", cp_id, f"Carregador mandou {acao}", acao=acao, bruto=texto)
        try:
            resposta = await run_in_threadpool(ocpp_handlers.tratar_chamada, cp_id, acao, payload)
            saida = [CALLRESULT, msg_id, resposta]
        except ocpp_handlers.AcaoNaoSuportada:
            saida = [CALLERROR, msg_id, "NotImplemented", f"Acao {acao} nao suportada", {}]
        except Exception as exc:
            logging.exception("[OCPP %s] erro tratando %s", cp_id, acao)
            saida = [CALLERROR, msg_id, "InternalError", str(exc)[:200], {}]
        texto_saida = _json(saida)
        logging.info("[OCPP %s] >> %s", cp_id, texto_saida)
        diagnostico.registrar(
            "resposta_enviada",
            cp_id,
            f"Servidor respondeu {acao}" if saida[0] == CALLRESULT else f"Servidor recusou {acao}: {saida[2]}",
            nivel="info" if saida[0] == CALLRESULT else "aviso",
            acao=acao,
            bruto=texto_saida,
        )
        await conexao.websocket.send_text(texto_saida)
        return

    pendente = conexao.pendentes.pop(msg_id, None)
    if pendente is None:
        logging.warning("[OCPP %s] resposta sem chamada pendente (id=%s)", cp_id, msg_id)
        diagnostico.registrar("resposta_recebida", cp_id, "Resposta sem pedido correspondente", nivel="aviso", bruto=texto)
        return
    acao, futuro = pendente
    # set_result roda os callbacks na hora; no threadpool eles podem usar o banco.
    if tipo == CALLRESULT:
        payload = mensagem[2] if len(mensagem) > 2 and isinstance(mensagem[2], dict) else {}
        diagnostico.registrar("resposta_recebida", cp_id, f"Carregador respondeu {acao}", acao=acao, bruto=texto)
        await run_in_threadpool(futuro.set_result, payload)
    elif tipo == CALLERROR:
        codigo = mensagem[2] if len(mensagem) > 2 else "GenericError"
        descricao = mensagem[3] if len(mensagem) > 3 else ""
        diagnostico.registrar(
            "resposta_recebida", cp_id, f"Carregador deu erro em {acao}: {codigo}", nivel="erro", acao=acao, bruto=texto
        )
        await run_in_threadpool(futuro.set_exception, ErroOcpp(str(codigo), str(descricao)))
