"""Tela WhatsApp do painel (igual Admin > WhatsApp da CompactPay): conexao com
a Evolution API, tempos, textos das mensagens e o que cada cliente recebe."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from datetime import timedelta

from sqlalchemy import func

from app.api.v1.endpoints.admin import _busca, _paginar, exigir_admin, get_db
from app.models.carregador import Cliente, agora
from app.models.whatsapp import WhatsappCliente, WhatsappEnvio
from app.services import whatsapp_alertas as wa

router = APIRouter(prefix="/admin/whatsapp", tags=["whatsapp"], dependencies=[Depends(exigir_admin)])


class ConfigDados(BaseModel):
    ativo: bool | None = None
    evolution_url: str | None = None
    api_key: str | None = None  # vazio = manter a salva
    instancia: str | None = None
    minutos_offline: int | None = Field(default=None, ge=5, le=24 * 60)
    lembrete_horas: int | None = Field(default=None, ge=0, le=24 * 7)
    resumo_horario: str | None = None
    mensagem_offline: str | None = None
    mensagem_lembrete: str | None = None
    mensagem_voltou: str | None = None
    mensagem_resumo: str | None = None
    mensagem_estorno: str | None = None


class ClienteEscolha(BaseModel):
    ativo: bool = False
    recebe_lembrete: bool = False
    recebe_voltou: bool = False
    resumo_diario: bool = False
    resumo_horario: str | None = None


def _erro(exc: wa.WhatsappErro) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


def _serializar_cliente(cliente: Cliente, e: WhatsappCliente | None) -> dict:
    return {
        "id": cliente.id,
        "nome_empresa": cliente.nome_empresa,
        "telefone": cliente.telefone,
        "telefones_validos": wa.telefones_do_cliente(cliente),
        "ativo": bool(e and e.ativo),
        "recebe_lembrete": bool(e and e.recebe_lembrete),
        "recebe_voltou": bool(e and e.recebe_voltou),
        "resumo_diario": bool(e and e.resumo_diario),
        "resumo_horario": e.resumo_horario if e else None,
        "resumo_ultimo_dia": e.resumo_ultimo_dia if e else None,
    }


def _tela(db: Session) -> dict:
    config = wa.obter_config(db)
    envios = db.query(WhatsappEnvio).order_by(WhatsappEnvio.id.desc()).limit(40).all()
    ids_clientes = {e.cliente_id for e in envios if e.cliente_id}
    nomes = {c.id: c.nome_empresa for c in db.query(Cliente).filter(Cliente.id.in_(ids_clientes))} if ids_clientes else {}
    desde = agora() - timedelta(hours=24)
    por_status = dict(
        db.query(WhatsappEnvio.status, func.count(WhatsappEnvio.id)).filter(WhatsappEnvio.created_at >= desde).group_by(WhatsappEnvio.status).all()
    )
    chave = config.api_key or ""
    return {
        "config": {
            "ativo": config.ativo,
            "evolution_url": config.evolution_url,
            "instancia": config.instancia,
            "tem_api_key": bool(chave),
            "api_key_mascarada": f"{chave[:4]}••••{chave[-4:]}" if len(chave) > 8 else ("••••" if chave else None),
            "minutos_offline": config.minutos_offline,
            "lembrete_horas": config.lembrete_horas,
            "resumo_horario": config.resumo_horario,
            "mensagens": {tipo: wa.modelo_mensagem(config, tipo) for tipo in wa.PADROES},
            "padroes": wa.PADROES,
            "variaveis": wa.VARIAVEIS,
            "ultimo_ciclo_em": config.ultimo_ciclo_em,
            "ultimo_ciclo_status": config.ultimo_ciclo_status,
        },
        "conexao": wa.conexao_para_tela(config),
        "contadores": {
            "enviados_24h": por_status.get("enviado", 0),
            "erros_24h": por_status.get("erro", 0),
            "clientes_avisados": db.query(WhatsappCliente).filter(WhatsappCliente.ativo.is_(True)).count(),
        },
        "envios": [
            {
                "id": envio.id,
                "cliente": nomes.get(envio.cliente_id),
                "tipo": envio.tipo,
                "telefone": envio.telefone,
                "mensagem": envio.mensagem,
                "status": envio.status,
                "erro": envio.erro,
                "created_at": envio.created_at,
            }
            for envio in envios
        ],
    }


@router.get("")
def tela(db: Session = Depends(get_db)):
    return _tela(db)


@router.put("/config")
def salvar_config(dados: ConfigDados, db: Session = Depends(get_db)):
    config = wa.obter_config(db)
    campos = dados.model_dump(exclude_unset=True)
    if "evolution_url" in campos:
        config.evolution_url = (campos.pop("evolution_url") or "").strip().rstrip("/") or None
    chave = (campos.pop("api_key", None) or "").strip()
    if chave:
        config.api_key = chave
    if "instancia" in campos:
        config.instancia = (campos.pop("instancia") or "").strip() or None
    if "resumo_horario" in campos:
        try:
            config.resumo_horario = wa.validar_hora(campos.pop("resumo_horario"), "Horario do resumo")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    for tipo in wa.PADROES:
        campo = f"mensagem_{tipo}"
        if campo in campos:
            texto = (campos.pop(campo) or "").strip()
            # Igual ao padrao fica vazio, para acompanhar melhorias do texto padrao.
            setattr(config, campo, None if not texto or texto == wa.PADROES[tipo].strip() else texto)
    if "ativo" in campos:
        ativo = campos.pop("ativo")
        if ativo and not wa.configurado(config):
            raise HTTPException(status_code=422, detail="Configure URL, chave e instancia da Evolution API antes de ativar")
        if ativo and not config.ativo:
            config.ativado_em = agora()
        config.ativo = ativo
    for campo, valor in campos.items():
        setattr(config, campo, valor)
    config.updated_at = agora()
    db.commit()
    return _tela(db)


@router.get("/clientes")
def listar_clientes(pagina: int = 1, por_pagina: int = 25, busca: str | None = None, db: Session = Depends(get_db)):
    """Quem recebe os avisos, paginado (o numero vem do cadastro do cliente)."""
    query = _busca(db.query(Cliente), busca, Cliente.nome_empresa, Cliente.telefone)
    resultado = _paginar(query, pagina, por_pagina, Cliente.nome_empresa.asc())
    ids = [c.id for c in resultado["itens"]]
    escolhas = {e.cliente_id: e for e in db.query(WhatsappCliente).filter(WhatsappCliente.cliente_id.in_(ids))} if ids else {}
    resultado["itens"] = [_serializar_cliente(c, escolhas.get(c.id)) for c in resultado["itens"]]
    return resultado


class TesteNumero(BaseModel):
    telefone: str


@router.post("/teste")
def testar_numero(dados: TesteNumero, db: Session = Depends(get_db)):
    try:
        return {"telefone": wa.enviar_teste_numero(db, dados.telefone)}
    except wa.WhatsappErro as exc:
        raise _erro(exc) from exc


@router.post("/conectar")
def conectar(db: Session = Depends(get_db)):
    try:
        return wa.conectar(wa.obter_config(db))
    except wa.WhatsappErro as exc:
        raise _erro(exc) from exc


@router.put("/clientes/{cliente_id}")
def salvar_cliente(cliente_id: int, dados: ClienteEscolha, db: Session = Depends(get_db)):
    if db.get(Cliente, cliente_id) is None:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    horario = None
    if dados.resumo_horario:
        try:
            horario = wa.validar_hora(dados.resumo_horario, "Horario do resumo")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    escolha = db.get(WhatsappCliente, cliente_id) or WhatsappCliente(cliente_id=cliente_id)
    if dados.ativo and not escolha.ativo:
        escolha.ativado_em = agora()
    escolha.ativo = dados.ativo
    escolha.recebe_lembrete = dados.recebe_lembrete
    escolha.recebe_voltou = dados.recebe_voltou
    escolha.resumo_diario = dados.resumo_diario
    escolha.resumo_horario = horario
    escolha.updated_at = agora()
    db.add(escolha)
    db.commit()
    return _tela(db)


def _cliente(db: Session, cliente_id: int) -> Cliente:
    cliente = db.get(Cliente, cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    return cliente


@router.post("/clientes/{cliente_id}/teste")
def testar_cliente(cliente_id: int, db: Session = Depends(get_db)):
    try:
        return {"enviados": wa.enviar_teste(db, _cliente(db, cliente_id))}
    except wa.WhatsappErro as exc:
        raise _erro(exc) from exc


@router.post("/clientes/{cliente_id}/resumo")
def resumo_agora(cliente_id: int, db: Session = Depends(get_db)):
    try:
        return {"enviados": wa.enviar_resumo_agora(db, _cliente(db, cliente_id))}
    except wa.WhatsappErro as exc:
        raise _erro(exc) from exc
