"""Avisos no WhatsApp pela Evolution API, no mesmo modelo da CompactPay
(services/whatsapp_alertas.py de la). Cada cliente escolhe na tela WhatsApp
o que recebe:

- offline: carregador sem conexao por mais de X minutos (padrao 30). Os do
  mesmo cliente que cairam juntos vao numa mensagem so.
- lembrete: se continuar offline, novo aviso a cada X horas (padrao 24).
- voltou: quando o carregador avisado reconecta.
- resumo diario: faturamento, recargas e energia do dia, no horario escolhido.

Um aviso por queda (carregador + ultimo_sinal). Uma instancia so do backend
(o OCPP ja exige isso), entao nao precisa da trava entre processos.
WhatsApp desconectado: o ciclo e' pulado sem marcar nada e tenta no proximo.
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta

import httpx

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cliente, agora
from app.models.whatsapp import WhatsappCliente, WhatsappConfig, WhatsappEnvio, WhatsappOfflineAviso
from app.services import ocpp_server, relatorios

FUSO = timedelta(hours=3)
PAUSA_ENTRE_MENSAGENS_SEGUNDOS = 3
_HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

PADROES = {
    "offline": (
        "⚠️ *CompactRecarga* | Carregador sem sinal\n\n"
        "Olá, {cliente}!\n\n"
        "Sem conexão ({quantidade}):\n"
        "{carregadores}\n\n"
        "Confira se o carregador está ligado e se o Wi-Fi do local está funcionando."
    ),
    "lembrete": (
        "🔔 *CompactRecarga* | Lembrete\n\n"
        "Olá, {cliente}! Continuam sem conexão ({quantidade}):\n"
        "{carregadores}\n\n"
        "Se já está resolvendo, pode desconsiderar."
    ),
    "voltou": "✅ *CompactRecarga*\n\nVoltaram a ficar online ({quantidade}):\n{carregadores}",
    "resumo": (
        "📊 *CompactRecarga* | Resumo de {data}\n\n"
        "Olá, {cliente}!\n\n"
        "{carregadores}\n\n"
        "*Total do dia: {total}*\n"
        "{vendas} venda(s) · {recargas} recarga(s) · {energia}"
    ),
}
VARIAVEIS = {
    "offline": ["cliente", "quantidade", "carregadores"],
    "lembrete": ["cliente", "quantidade", "carregadores"],
    "voltou": ["cliente", "quantidade", "carregadores"],
    "resumo": ["cliente", "data", "carregadores", "total", "vendas", "recargas", "energia"],
}
_CAMPO_MENSAGEM = {tipo: f"mensagem_{tipo}" for tipo in PADROES}


class WhatsappErro(Exception):
    def __init__(self, mensagem: str, http_status: int | None = None):
        super().__init__(mensagem)
        self.http_status = http_status


# ---- Config ---------------------------------------------------------------

def obter_config(db) -> WhatsappConfig:
    config = db.get(WhatsappConfig, 1)
    if config is None:
        config = WhatsappConfig(id=1, ativo=False, minutos_offline=30, lembrete_horas=24, resumo_horario="22:00")
        db.add(config)
        db.commit()
    return config


def modelo_mensagem(config: WhatsappConfig, tipo: str) -> str:
    return getattr(config, _CAMPO_MENSAGEM[tipo]) or PADROES[tipo]


def validar_hora(valor, campo: str = "horario") -> str:
    valor = str(valor or "").strip()
    if not _HORA_RE.match(valor):
        raise ValueError(f"{campo} invalido: use HH:MM")
    return valor


def configurado(config: WhatsappConfig) -> bool:
    return bool(config.evolution_url and config.api_key and config.instancia)


# ---- Evolution API --------------------------------------------------------

def _chamar(config: WhatsappConfig, caminho: str, method: str = "GET", body: dict | None = None):
    if not config.evolution_url or not config.api_key:
        raise WhatsappErro("Configure a URL e a chave da Evolution API.")
    try:
        resposta = httpx.request(
            method,
            f"{config.evolution_url}{caminho}",
            headers={"apikey": config.api_key, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise WhatsappErro(f"Nao foi possivel falar com a Evolution API: {exc}") from exc
    try:
        dados = resposta.json() if resposta.content else None
    except ValueError:
        dados = {"message": resposta.text}
    if resposta.status_code >= 400:
        bruto = None
        if isinstance(dados, dict):
            bruto = (dados.get("response") or {}).get("message") if isinstance(dados.get("response"), dict) else None
            bruto = bruto or dados.get("message") or dados.get("error")
        detalhe = "; ".join(map(str, bruto)) if isinstance(bruto, list) else str(bruto or resposta.reason_phrase)
        if resposta.status_code in (401, 403):
            mensagem = "A Evolution API recusou a chave (apikey). Confira a configuracao."
        elif re.search(r"'exists':\s*False|\"exists\":\s*false", detalhe):
            mensagem = "Este numero nao tem WhatsApp."
        else:
            mensagem = f"Evolution API respondeu {resposta.status_code}: {detalhe}"
        raise WhatsappErro(mensagem, resposta.status_code)
    return dados


def estado_conexao(config: WhatsappConfig) -> str:
    dados = _chamar(config, f"/instance/connectionState/{config.instancia}") or {}
    return (dados.get("instance") or {}).get("state") or dados.get("state") or "unknown"


def conexao_para_tela(config: WhatsappConfig) -> dict:
    if not configurado(config):
        return {"configurado": False, "estado": None}
    try:
        return {"configurado": True, "estado": estado_conexao(config)}
    except WhatsappErro as exc:
        if exc.http_status == 404:  # instancia e' criada ao conectar
            return {"configurado": True, "estado": "inexistente"}
        return {"configurado": True, "estado": "erro", "erro": str(exc)}


def conectar(config: WhatsappConfig) -> dict:
    """QR code para conectar o numero. Cria a instancia se ainda nao existir."""
    if not configurado(config):
        raise WhatsappErro("Salve URL, chave e instancia da Evolution API antes de conectar.")
    try:
        estado = estado_conexao(config)
    except WhatsappErro as exc:
        if exc.http_status != 404:
            raise
        estado = "inexistente"
    if estado == "open":
        return {"estado": estado, "qr_base64": None, "pairing_code": None}
    if estado == "inexistente":
        dados = _chamar(
            config, "/instance/create", method="POST", body={"instanceName": config.instancia, "integration": "WHATSAPP-BAILEYS", "qrcode": True}
        ) or {}
        dados = dados.get("qrcode") or dados
    else:
        dados = _chamar(config, f"/instance/connect/{config.instancia}") or {}
    base64 = dados.get("base64")
    if base64 and not base64.startswith("data:"):
        base64 = f"data:image/png;base64,{base64}"
    return {"estado": "connecting", "qr_base64": base64, "pairing_code": dados.get("pairingCode")}


def enviar_texto(config: WhatsappConfig, numero: str, texto: str) -> None:
    _chamar(config, f"/message/sendText/{config.instancia}", method="POST", body={"number": numero, "text": texto, "delay": 1200})


# ---- Mensagens ------------------------------------------------------------

def normalizar_telefone(telefone: str | None) -> str | None:
    digitos = re.sub(r"\D", "", str(telefone or "")).lstrip("0")
    if len(digitos) in (10, 11):
        digitos = f"55{digitos}"
    return digitos if re.fullmatch(r"55\d{10,11}", digitos) else None


def telefones_do_cliente(cliente: Cliente) -> list[str]:
    """Um ou mais numeros no campo telefone do cliente (separados por virgula)."""
    numeros = []
    for bruto in re.split(r"[,;/]", cliente.telefone or ""):
        numero = normalizar_telefone(bruto)
        if numero and numero not in numeros:
            numeros.append(numero)
    return numeros


def formatar_duracao(delta: timedelta) -> str:
    minutos = max(0, int(delta.total_seconds() // 60))
    dias, resto = divmod(minutos, 24 * 60)
    horas, minutos = divmod(resto, 60)
    partes = []
    if dias:
        partes.append(f"{dias} dia" + ("s" if dias > 1 else ""))
    if horas:
        partes.append(f"{horas}h")
    if minutos or not partes:
        partes.append(f"{minutos}min")
    return " ".join(partes)


def formatar_moeda(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def montar_mensagem(modelo: str, valores: dict) -> str:
    """Troca {variavel}; chave desconhecida fica como esta (sem quebrar)."""
    return re.sub(r"\{(\w+)\}", lambda m: str(valores.get(m.group(1), m.group(0))), modelo)


def _valores_grupo(cliente: Cliente, itens: list[tuple[Carregador, datetime]], momento: datetime, tipo: str) -> dict:
    linhas = []
    for carregador, desde in itens:
        if tipo == "voltou":
            linhas.append(f"• {carregador.nome_local} (ficou {formatar_duracao(momento - desde)} fora)")
        else:
            linhas.append(f"• {carregador.nome_local} ({carregador.id_hardware}) — sem sinal há {formatar_duracao(momento - desde)}")
    return {"cliente": cliente.nome_empresa, "quantidade": len(itens), "carregadores": "\n".join(linhas)}


def valores_resumo(db, cliente: Cliente, momento: datetime) -> dict:
    dados = relatorios.gerar(db, "hoje", None, None, cliente.id, None, False)
    linhas = [
        f"• {r['nome_local']}: {formatar_moeda(r['faturamento'])} — {r['recargas']} recarga(s), {r['energia_kwh']:.1f} kWh"
        for r in dados["ranking"]
    ]
    totais = dados["totais"]
    return {
        "cliente": cliente.nome_empresa,
        "data": (momento - FUSO).strftime("%d/%m/%Y"),
        "carregadores": "\n".join(linhas) or "Nenhum carregador.",
        "total": formatar_moeda(totais["faturamento"]),
        "vendas": totais["vendas"],
        "recargas": totais["recargas"],
        "energia": f"{totais['energia_kwh']:.1f} kWh",
    }


def _enviar_para_cliente(db, config, cliente: Cliente, tipo: str, texto: str, carregador_ids: list[str]) -> int:
    """Envia para cada telefone do cliente e grava o historico. Devolve quantos foram."""
    enviados = 0
    for i, numero in enumerate(telefones_do_cliente(cliente)):
        if i:
            time.sleep(PAUSA_ENTRE_MENSAGENS_SEGUNDOS)
        envio = WhatsappEnvio(cliente_id=cliente.id, carregador_ids=",".join(carregador_ids), tipo=tipo, telefone=numero, mensagem=texto)
        try:
            enviar_texto(config, numero, texto)
            envio.status = "enviado"
            enviados += 1
        except WhatsappErro as exc:
            envio.status = "erro"
            envio.erro = str(exc)[:500]
        db.add(envio)
        db.commit()
    return enviados


def enviar_teste(db, cliente: Cliente) -> int:
    config = obter_config(db)
    if not telefones_do_cliente(cliente):
        raise WhatsappErro("Cliente sem telefone valido no cadastro.")
    texto = f"✅ *CompactRecarga*\n\nTeste de aviso para {cliente.nome_empresa}. Se chegou, esta tudo certo!"
    enviados = _enviar_para_cliente(db, config, cliente, "teste", texto, [])
    if not enviados:
        raise WhatsappErro(_ultimo_erro(db, cliente) or "Nao foi possivel enviar.")
    return enviados


def enviar_resumo_agora(db, cliente: Cliente) -> int:
    """Resumo de hoje fora do horario (botao da tela); nao mexe no "ja enviado hoje"."""
    config = obter_config(db)
    if not telefones_do_cliente(cliente):
        raise WhatsappErro("Cliente sem telefone valido no cadastro.")
    ids = [c.id_hardware for c in db.query(Carregador).filter(Carregador.cliente_id == cliente.id).all()]
    texto = montar_mensagem(modelo_mensagem(config, "resumo"), valores_resumo(db, cliente, agora()))
    enviados = _enviar_para_cliente(db, config, cliente, "resumo", texto, ids)
    if not enviados:
        raise WhatsappErro(_ultimo_erro(db, cliente) or "Nao foi possivel enviar o resumo.")
    return enviados


def _ultimo_erro(db, cliente: Cliente) -> str | None:
    envio = (
        db.query(WhatsappEnvio).filter(WhatsappEnvio.cliente_id == cliente.id, WhatsappEnvio.status == "erro").order_by(WhatsappEnvio.id.desc()).first()
    )
    return envio.erro if envio else None


# ---- Ciclo do worker ------------------------------------------------------

_PULADO_TEXTO = {
    "desativado": "Envio automatico desativado",
    "sem_clientes": "Nenhum cliente marcado para receber",
    "desconectado": "WhatsApp desconectado: nada foi enviado",
}


def descrever_ciclo(resultado: dict) -> str:
    if resultado.get("erros"):
        return "Erro: " + " | ".join(resultado["erros"])
    if resultado.get("pulado"):
        return _PULADO_TEXTO.get(resultado["pulado"], resultado["pulado"])
    enviados = {t: resultado[t] for t in ("offline", "lembrete", "voltou", "resumo") if resultado.get(t)}
    if not enviados:
        return "OK, nada para enviar agora"
    return "OK, enviado: " + ", ".join(f"{qtd} {tipo}" for tipo, qtd in enviados.items())


def _online(carregador: Carregador) -> bool:
    return ocpp_server.esta_conectado(carregador.id_hardware)


def verificar_e_enviar(db, momento: datetime | None = None) -> dict:
    momento = momento or agora()
    resultado = {"offline": 0, "lembrete": 0, "voltou": 0, "resumo": 0, "pulado": None}
    config = obter_config(db)
    if not config.ativo:
        resultado["pulado"] = "desativado"
    else:
        try:
            _ciclo(db, config, momento, resultado)
        except Exception as exc:
            logging.exception("[whatsapp] ciclo falhou")
            db.rollback()
            resultado.setdefault("erros", []).append(str(exc)[:200])
    config = obter_config(db)
    config.ultimo_ciclo_em = agora()
    config.ultimo_ciclo_status = descrever_ciclo(resultado)[:500]
    db.commit()
    return resultado


def _ciclo(db, config, momento, resultado) -> None:
    escolhas = {e.cliente_id: e for e in db.query(WhatsappCliente).all()}
    abertos = db.query(WhatsappOfflineAviso).filter(WhatsappOfflineAviso.resolvido_em.is_(None)).all()
    if not any(e.ativo or e.resumo_diario for e in escolhas.values()) and not abertos:
        resultado["pulado"] = "sem_clientes"
        return
    try:
        estado = estado_conexao(config)
    except WhatsappErro as exc:
        estado = str(exc)
    if estado != "open":
        logging.warning("[whatsapp] envio pausado, conexao: %s", estado)
        resultado["pulado"] = "desconectado"
        return

    carregadores = db.query(Carregador).filter(Carregador.cliente_id.isnot(None), Carregador.ativo.is_(True)).all()
    por_id = {c.id_hardware: c for c in carregadores}
    etapas = [
        ("voltou", lambda: _avisar_voltou(db, config, momento, escolhas, abertos, por_id, resultado)),
        ("offline", lambda: _avisar_quedas(db, config, momento, escolhas, carregadores, resultado)),
        ("lembrete", lambda: _avisar_lembretes(db, config, momento, escolhas, abertos, por_id, resultado)),
        ("resumo", lambda: _enviar_resumos(db, config, momento, escolhas, resultado)),
    ]
    for nome, etapa in etapas:
        try:
            etapa()
        except Exception as exc:
            logging.exception("[whatsapp] etapa %s falhou", nome)
            db.rollback()
            resultado.setdefault("erros", []).append(f"{nome}: {exc}"[:200])


def _avisar_voltou(db, config, momento, escolhas, abertos, por_id, resultado) -> None:
    grupos: dict[int, list] = {}
    for aviso in abertos:
        carregador = por_id.get(aviso.carregador_id)
        if carregador is None:
            aviso.resolvido_em = momento
            continue
        if _online(carregador):
            aviso.resolvido_em = momento
            escolha = escolhas.get(carregador.cliente_id)
            if escolha and escolha.ativo and escolha.recebe_voltou:
                grupos.setdefault(carregador.cliente_id, []).append((carregador, aviso.offline_desde))
        elif carregador.ultimo_sinal and carregador.ultimo_sinal > aviso.offline_desde:
            aviso.resolvido_em = momento  # voltou e caiu de novo entre dois ciclos: outra queda
    db.commit()
    for itens in grupos.values():
        cliente = itens[0][0].cliente
        texto = montar_mensagem(modelo_mensagem(config, "voltou"), _valores_grupo(cliente, itens, momento, "voltou"))
        if _enviar_para_cliente(db, config, cliente, "voltou", texto, [c.id_hardware for c, _ in itens]):
            resultado["voltou"] += 1


def _avisar_quedas(db, config, momento, escolhas, carregadores, resultado) -> None:
    limite = timedelta(minutes=config.minutos_offline or 30)
    candidatos = [c for c in carregadores if c.ultimo_sinal and escolhas.get(c.cliente_id) and escolhas[c.cliente_id].ativo]
    if not candidatos:
        return
    ja_avisados = set(
        db.query(WhatsappOfflineAviso.carregador_id, WhatsappOfflineAviso.offline_desde).filter(
            WhatsappOfflineAviso.carregador_id.in_([c.id_hardware for c in candidatos])
        )
    )
    grupos: dict[int, list] = {}
    for carregador in candidatos:
        if _online(carregador) or (carregador.id_hardware, carregador.ultimo_sinal) in ja_avisados:
            continue
        if momento - carregador.ultimo_sinal < limite:
            continue
        # Ja estava offline antes de ligar o aviso (geral ou do cliente): nao manda.
        escolha = escolhas[carregador.cliente_id]
        ativado_em = max(filter(None, [config.ativado_em, escolha.ativado_em]), default=None)
        if ativado_em and carregador.ultimo_sinal + limite < ativado_em:
            continue
        grupos.setdefault(carregador.cliente_id, []).append((carregador, carregador.ultimo_sinal))

    for itens in grupos.values():
        cliente = itens[0][0].cliente
        # Registra a queda antes de enviar: se o envio travar no meio, nao repete.
        for carregador, desde in itens:
            db.add(WhatsappOfflineAviso(carregador_id=carregador.id_hardware, cliente_id=cliente.id, offline_desde=desde, avisado_em=momento, lembretes=0))
        db.commit()
        texto = montar_mensagem(modelo_mensagem(config, "offline"), _valores_grupo(cliente, itens, momento, "offline"))
        if _enviar_para_cliente(db, config, cliente, "offline", texto, [c.id_hardware for c, _ in itens]):
            resultado["offline"] += 1


def _avisar_lembretes(db, config, momento, escolhas, abertos, por_id, resultado) -> None:
    if not config.lembrete_horas:
        return
    intervalo = timedelta(hours=config.lembrete_horas)
    grupos: dict[int, list] = {}
    for aviso in abertos:
        if aviso.resolvido_em is not None:
            continue
        carregador = por_id.get(aviso.carregador_id)
        escolha = escolhas.get(carregador.cliente_id) if carregador else None
        if not escolha or not (escolha.ativo and escolha.recebe_lembrete) or _online(carregador):
            continue
        if momento - (aviso.ultimo_lembrete_em or aviso.avisado_em) < intervalo:
            continue
        grupos.setdefault(carregador.cliente_id, []).append((carregador, aviso))

    for itens in grupos.values():
        cliente = itens[0][0].cliente
        for _, aviso in itens:
            aviso.ultimo_lembrete_em = momento
            aviso.lembretes = (aviso.lembretes or 0) + 1
        db.commit()
        pares = [(c, aviso.offline_desde) for c, aviso in itens]
        texto = montar_mensagem(modelo_mensagem(config, "lembrete"), _valores_grupo(cliente, pares, momento, "lembrete"))
        if _enviar_para_cliente(db, config, cliente, "lembrete", texto, [c.id_hardware for c, _ in pares]):
            resultado["lembrete"] += 1


def _enviar_resumos(db, config, momento, escolhas, resultado) -> None:
    local = momento - FUSO
    hoje, hora = local.date().isoformat(), local.strftime("%H:%M")
    for cliente_id, escolha in escolhas.items():
        if not escolha.resumo_diario or escolha.resumo_ultimo_dia == hoje:
            continue
        if hora < (escolha.resumo_horario or config.resumo_horario or "22:00"):
            continue
        cliente = db.get(Cliente, cliente_id)
        if cliente is None:
            continue
        ids = [c.id_hardware for c in db.query(Carregador).filter(Carregador.cliente_id == cliente_id).all()]
        texto = montar_mensagem(modelo_mensagem(config, "resumo"), valores_resumo(db, cliente, momento))
        # Marca antes de enviar: uma vez por dia, mesmo se falhar no meio.
        escolha.resumo_ultimo_dia = hoje
        db.commit()
        if _enviar_para_cliente(db, config, cliente, "resumo", texto, ids):
            resultado["resumo"] += 1


def _rodar() -> None:
    logging.info("Worker de WhatsApp iniciado")
    while True:
        db = SessionLocal()
        try:
            resultado = verificar_e_enviar(db)
            if any(resultado[t] for t in ("offline", "lembrete", "voltou", "resumo")):
                logging.info("[whatsapp] mensagens enviadas: %s", resultado)
        except Exception:
            logging.exception("[whatsapp] ciclo de avisos falhou")
            db.rollback()
        finally:
            db.close()
        time.sleep(settings.WHATSAPP_INTERVALO_SEGUNDOS)


def start_whatsapp_worker() -> threading.Thread | None:
    if not settings.START_WHATSAPP_WORKER:
        logging.info("Worker de WhatsApp desativado por START_WHATSAPP_WORKER=false")
        return None
    thread = threading.Thread(target=_rodar, daemon=True)
    thread.start()
    return thread
