"""Administracao (header X-Admin-Key = ADMIN_API_KEY): cadastro de
carregador, pacotes e tablets, sessoes e comandos OCPP de diagnostico.
Fica assim ate o painel ser integrado ao frontend da CompactPay."""
import concurrent.futures
import hmac
import socket
from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cliente, Cobranca, HistoricoOperacao, PacoteTempo, SessaoCarga, TerminalTablet, agora
from app.services import diagnostico, mercado_pago_caixa, ocpp_server, pagamentos, sessoes, terminais
from app.services.ocpp_handlers import STATUS_SESSAO_ATIVA, energia_kwh
from app.api.v1.endpoints.tablet import serializar_sessao

router = APIRouter(prefix="/admin", tags=["admin"])

OCPP_RESPOSTA_TIMEOUT_SECONDS = 15


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def exigir_admin(x_admin_key: str | None = Header(default=None)) -> None:
    if not settings.ADMIN_API_KEY or not x_admin_key or not hmac.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Chave de admin invalida")


class CarregadorCreate(BaseModel):
    id_hardware: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_\-.]+$")
    nome_local: str = Field(min_length=1, max_length=80)
    connector_id: int = 1
    mp_terminal_id: str | None = None
    mp_access_token: str | None = None
    cliente_id: int | None = None
    gerar_senha_ocpp: bool = False


class CarregadorUpdate(BaseModel):
    nome_local: str | None = None
    connector_id: int | None = None
    mp_terminal_id: str | None = None
    mp_access_token: str | None = None
    ativo: bool | None = None
    cliente_id: int | None = None
    preco_valor: float | None = Field(default=None, gt=0)
    preco_minutos: int | None = Field(default=None, gt=0, le=24 * 60)


class ClienteDados(BaseModel):
    nome_empresa: str | None = Field(default=None, min_length=1, max_length=80)
    email_contato: str | None = None
    telefone: str | None = None
    documento: str | None = None
    endereco_rua: str | None = None
    endereco_numero: str | None = None
    endereco_cidade: str | None = None
    endereco_estado: str | None = None
    endereco_latitude: float | None = None
    endereco_longitude: float | None = None
    # Vazio na edicao = manter o token salvo.
    mp_access_token: str | None = None
    ativo: bool | None = None


class EnderecoLoja(BaseModel):
    """Endereco da loja no MP; vazio usa os MP_DEFAULT_STORE_* do servidor."""
    rua: str | None = None
    numero: str | None = None
    cidade: str | None = None
    estado: str | None = None


class PagamentoSimulado(BaseModel):
    valor: float = Field(gt=0)


class PacoteCreate(BaseModel):
    nome: str = Field(min_length=1, max_length=40)
    minutos: int = Field(gt=0, le=24 * 60)
    valor: float = Field(gt=0)
    ordem: int = 0


class TerminalCreate(BaseModel):
    nome: str = Field(min_length=1, max_length=80)


class SessaoTeste(BaseModel):
    minutos: int = Field(gt=0, le=24 * 60)


class ComandoOcpp(BaseModel):
    acao: str
    payload: dict = {}


def _serializar_carregador(carregador: Carregador) -> dict:
    return {
        "id_hardware": carregador.id_hardware,
        "nome_local": carregador.nome_local,
        "connector_id": carregador.connector_id,
        "mp_terminal_id": carregador.mp_terminal_id,
        "ativo": carregador.ativo,
        "online": ocpp_server.esta_conectado(carregador.id_hardware),
        "status_conector": carregador.status_conector,
        "erro_conector": carregador.erro_conector,
        "vendor": carregador.vendor,
        "modelo": carregador.modelo,
        "numero_serie": carregador.numero_serie,
        "firmware_version": carregador.firmware_version,
        "ultimo_sinal": carregador.ultimo_sinal,
        "tem_senha_ocpp": bool(carregador.ocpp_senha_hash),
        "cliente_id": carregador.cliente_id,
        "cliente_nome": carregador.cliente.nome_empresa if carregador.cliente else None,
        "tem_token_mp": bool(pagamentos.token_do_carregador(carregador)),
        "preco_valor": carregador.preco_valor,
        "preco_minutos": carregador.preco_minutos,
        "mp_store_id": carregador.mp_store_id,
        "mp_store_external_id": carregador.mp_store_external_id,
        "mp_pos_id": carregador.mp_pos_id,
        "mp_pos_external_id": carregador.mp_pos_external_id,
    }


def _obter(db: Session, carregador_id: str) -> Carregador:
    carregador = db.get(Carregador, carregador_id)
    if not carregador:
        raise HTTPException(status_code=404, detail="Carregador nao encontrado")
    return carregador


@router.post("/carregadores", dependencies=[Depends(exigir_admin)])
def criar_carregador(dados: CarregadorCreate, db: Session = Depends(get_db)):
    if db.get(Carregador, dados.id_hardware):
        raise HTTPException(status_code=409, detail="Carregador ja cadastrado")
    if dados.cliente_id is not None and db.get(Cliente, dados.cliente_id) is None:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    carregador = Carregador(
        id_hardware=dados.id_hardware,
        nome_local=dados.nome_local,
        connector_id=dados.connector_id,
        mp_terminal_id=dados.mp_terminal_id,
        mp_access_token=dados.mp_access_token,
        cliente_id=dados.cliente_id,
    )
    senha = None
    if dados.gerar_senha_ocpp:
        # Senha curta o bastante para digitar no app do fabricante.
        senha = terminais.gerar_chave("")[:20]
        carregador.ocpp_senha_hash = terminais.hash_chave(senha)
    db.add(carregador)
    db.commit()
    resposta = _serializar_carregador(carregador)
    resposta["senha_ocpp"] = senha  # aparece so agora
    return resposta


@router.get("/carregadores", dependencies=[Depends(exigir_admin)])
def listar_carregadores(db: Session = Depends(get_db)):
    resposta = []
    for carregador in db.query(Carregador).order_by(Carregador.id_hardware).all():
        item = _serializar_carregador(carregador)
        ativa = sessoes.sessao_ativa(db, carregador.id_hardware)
        item["sessao_ativa"] = serializar_sessao(ativa) if ativa else None
        resposta.append(item)
    return resposta


@router.get("/carregadores/{carregador_id}", dependencies=[Depends(exigir_admin)])
def obter_carregador(carregador_id: str, db: Session = Depends(get_db)):
    carregador = _obter(db, carregador_id)
    item = _serializar_carregador(carregador)
    ativa = sessoes.sessao_ativa(db, carregador_id)
    item["sessao_ativa"] = serializar_sessao(ativa) if ativa else None
    return item


@router.get("/carregadores/{carregador_id}/pacotes", dependencies=[Depends(exigir_admin)])
def listar_pacotes(carregador_id: str, db: Session = Depends(get_db)):
    _obter(db, carregador_id)
    pacotes = (
        db.query(PacoteTempo)
        .filter(PacoteTempo.carregador_id == carregador_id, PacoteTempo.ativo.is_(True))
        .order_by(PacoteTempo.ordem, PacoteTempo.valor)
        .all()
    )
    return [{"id": p.id, "nome": p.nome, "minutos": p.minutos, "valor": p.valor, "ordem": p.ordem} for p in pacotes]


@router.get("/resumo", dependencies=[Depends(exigir_admin)])
def resumo(db: Session = Depends(get_db)):
    """Numeros do dashboard. "Hoje" e' o dia em UTC-3 (Brasilia)."""
    inicio_dia = (agora() - timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=3)
    carregadores = db.query(Carregador).filter(Carregador.ativo.is_(True)).all()
    vendas = (
        db.query(Cobranca)
        .filter(
            Cobranca.status == "aprovada",
            Cobranca.pago_em >= inicio_dia,
            Cobranca.estornada_em.is_(None),
            Cobranca.origem.in_(("point", "simulado")),
        )
        .all()
    )
    sessoes_hoje = db.query(SessaoCarga).filter(SessaoCarga.inicio >= inicio_dia).all()
    return {
        "carregadores_total": len(carregadores),
        "carregadores_online": sum(1 for c in carregadores if ocpp_server.esta_conectado(c.id_hardware)),
        "sessoes_ativas": db.query(SessaoCarga).filter(SessaoCarga.status.in_(STATUS_SESSAO_ATIVA)).count(),
        "faturamento_hoje": round(sum(v.valor for v in vendas), 2),
        "vendas_hoje": len(vendas),
        "energia_hoje_kwh": round(sum(energia_kwh(s) for s in sessoes_hoje), 3),
        "estornos_pendentes": db.query(Cobranca).filter(Cobranca.estorno_erro.isnot(None), Cobranca.estornada_em.is_(None)).count(),
    }


@router.patch("/carregadores/{carregador_id}", dependencies=[Depends(exigir_admin)])
def atualizar_carregador(carregador_id: str, dados: CarregadorUpdate, db: Session = Depends(get_db)):
    carregador = _obter(db, carregador_id)
    campos = dados.model_dump(exclude_unset=True)
    if campos.get("cliente_id") is not None and db.get(Cliente, campos["cliente_id"]) is None:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    if campos.get("cliente_id") != carregador.cliente_id and "cliente_id" in campos and carregador.mp_pos_id:
        # O caixa e' da conta MP do cliente antigo; outro dono precisa de caixa novo.
        carregador.mp_user_id = carregador.mp_store_id = carregador.mp_store_external_id = None
        carregador.mp_pos_id = carregador.mp_pos_external_id = None
    for campo, valor in campos.items():
        setattr(carregador, campo, valor)
    db.commit()
    db.refresh(carregador)
    return _serializar_carregador(carregador)


@router.get("/carregadores/{carregador_id}/historico", dependencies=[Depends(exigir_admin)])
def historico_carregador(carregador_id: str, limite: int = 100, db: Session = Depends(get_db)):
    itens = (
        db.query(HistoricoOperacao)
        .filter(HistoricoOperacao.carregador_id == carregador_id)
        .order_by(HistoricoOperacao.id.desc())
        .limit(min(limite, 500))
        .all()
    )
    return [
        {"categoria": h.categoria, "descricao": h.descricao, "valor": h.valor, "created_at": h.created_at}
        for h in itens
    ]


@router.post("/carregadores/{carregador_id}/pacotes", dependencies=[Depends(exigir_admin)])
def criar_pacote(carregador_id: str, dados: PacoteCreate, db: Session = Depends(get_db)):
    _obter(db, carregador_id)
    pacote = PacoteTempo(carregador_id=carregador_id, **dados.model_dump())
    db.add(pacote)
    db.commit()
    return {"id": pacote.id, **dados.model_dump()}


@router.delete("/pacotes/{pacote_id}", dependencies=[Depends(exigir_admin)])
def desativar_pacote(pacote_id: int, db: Session = Depends(get_db)):
    pacote = db.get(PacoteTempo, pacote_id)
    if not pacote:
        raise HTTPException(status_code=404, detail="Pacote nao encontrado")
    pacote.ativo = False  # cobrancas antigas continuam apontando para ele
    db.commit()
    return {"ok": True}


@router.post("/terminais", dependencies=[Depends(exigir_admin)])
def criar_terminal(dados: TerminalCreate, db: Session = Depends(get_db)):
    chave = terminais.gerar_chave()
    terminal = TerminalTablet(nome=dados.nome, chave_prefixo=chave[:8], chave_hash=terminais.hash_chave(chave))
    db.add(terminal)
    db.commit()
    return {"id": terminal.id, "nome": terminal.nome, "chave": chave}  # chave aparece so agora


@router.get("/terminais", dependencies=[Depends(exigir_admin)])
def listar_terminais(db: Session = Depends(get_db)):
    return [
        {
            "id": t.id,
            "nome": t.nome,
            "chave_prefixo": t.chave_prefixo,
            "ativo": t.ativo,
            "created_at": t.created_at,
            "ultimo_uso_em": t.ultimo_uso_em,
        }
        for t in db.query(TerminalTablet).order_by(TerminalTablet.id.desc()).all()
    ]


@router.delete("/terminais/{terminal_id}", dependencies=[Depends(exigir_admin)])
def revogar_terminal(terminal_id: int, db: Session = Depends(get_db)):
    terminal = db.get(TerminalTablet, terminal_id)
    if not terminal:
        raise HTTPException(status_code=404, detail="Terminal nao encontrado")
    terminal.ativo = False
    db.commit()
    return {"ok": True}


@router.get("/sessoes", dependencies=[Depends(exigir_admin)])
def listar_sessoes(carregador_id: str | None = None, limite: int = 50, db: Session = Depends(get_db)):
    query = db.query(SessaoCarga)
    if carregador_id:
        query = query.filter(SessaoCarga.carregador_id == carregador_id)
    lista = query.order_by(SessaoCarga.id.desc()).limit(min(limite, 500)).all()
    cobrancas = {c.id: c for c in db.query(Cobranca).filter(Cobranca.id.in_([s.cobranca_id for s in lista])).all()}
    resposta = []
    for sessao in lista:
        cobranca = cobrancas.get(sessao.cobranca_id)
        resposta.append(
            {
                **serializar_sessao(sessao),
                "created_at": sessao.created_at,
                "valor": cobranca.valor if cobranca else None,
                "origem": cobranca.origem if cobranca else None,
                "metodo": cobranca.metodo if cobranca else None,
                "estornada": bool(cobranca and cobranca.estornada_em),
                "estorno_erro": bool(cobranca and cobranca.estorno_erro and not cobranca.estornada_em),
            }
        )
    return resposta


@router.post("/sessoes/{sessao_id}/encerrar", dependencies=[Depends(exigir_admin)])
def encerrar_sessao(sessao_id: int, db: Session = Depends(get_db)):
    sessao = db.get(SessaoCarga, sessao_id)
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessao nao encontrada")
    sessoes.encerrar_sessao(db, sessao, "encerrada_pelo_admin")
    db.refresh(sessao)
    return serializar_sessao(sessao)


@router.post("/carregadores/{carregador_id}/sessao-teste", dependencies=[Depends(exigir_admin)])
def sessao_teste(carregador_id: str, dados: SessaoTeste, db: Session = Depends(get_db)):
    """Libera N minutos sem pagamento (teste de bancada). Passa pelo mesmo
    caminho de uma cobranca aprovada."""
    carregador = _obter(db, carregador_id)
    if sessoes.sessao_ativa(db, carregador_id) is not None:
        raise HTTPException(status_code=409, detail="Carregador em uso")
    # Pacote so em memoria (nao vai para o banco nem aparece no tablet).
    pacote_teste = PacoteTempo(carregador_id=carregador_id, nome="teste", minutos=dados.minutos, valor=0)
    cobranca = pagamentos.criar_cobranca(db, carregador, pacote_teste, origem="teste")
    pagamentos.aprovar_cobranca(db, cobranca.id, metodo="teste")
    sessao = db.query(SessaoCarga).filter(SessaoCarga.cobranca_id == cobranca.id).first()
    return serializar_sessao(sessao)


@router.post("/carregadores/{carregador_id}/ocpp", dependencies=[Depends(exigir_admin)])
def comando_ocpp(carregador_id: str, dados: ComandoOcpp, db: Session = Depends(get_db)):
    """Manda qualquer chamada OCPP e devolve a resposta. Para diagnostico:
    GetConfiguration, ChangeConfiguration, Reset, UnlockConnector, TriggerMessage..."""
    _obter(db, carregador_id)
    futuro = ocpp_server.enviar_chamada(carregador_id, dados.acao, dados.payload)
    if futuro is None:
        raise HTTPException(status_code=409, detail="Carregador offline")
    try:
        return {"resposta": futuro.result(timeout=OCPP_RESPOSTA_TIMEOUT_SECONDS)}
    except concurrent.futures.TimeoutError:
        raise HTTPException(status_code=504, detail="Carregador nao respondeu")
    except ocpp_server.ErroOcpp as exc:
        raise HTTPException(status_code=502, detail=f"Carregador respondeu erro: {exc.codigo} {exc.descricao}")


# ---- Diagnostico (tela de testes do painel) ----------------------------

def _ips_locais() -> list[str]:
    """IPs do servidor na rede local, para a bancada (carregador na mesma rede).
    O primeiro e' o da placa que sai para a internet (o Wi-Fi), nao o de
    VPN ou maquina virtual."""
    principal = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))  # UDP: nao envia nada, so escolhe a rota
            principal = sock.getsockname()[0]
    except OSError:
        pass
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        ips = []
    outros = [ip for ip in ips if not ip.startswith("127.") and ip != principal]
    return ([principal] if principal else []) + outros


@router.get("/diagnostico/servidor", dependencies=[Depends(exigir_admin)])
def diagnostico_servidor(request: Request):
    """O que o painel precisa para montar a tela de diagnostico: endereco
    OCPP que o carregador deve usar, quem esta conectado e IDs recusados."""
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return {
        "versao": settings.APP_VERSION,
        "hora": agora(),
        "host": host,
        "ocpp_seguro": https,
        "ips_locais": _ips_locais(),
        "heartbeat_intervalo": settings.OCPP_HEARTBEAT_INTERVAL,
        "conectados": ocpp_server.conectados(),
        "ids_nao_cadastrados": diagnostico.ids_recusados_nao_cadastrados(),
        "ultimo_seq": diagnostico.ultimo_seq(),
    }


@router.get("/diagnostico/eventos", dependencies=[Depends(exigir_admin)])
def diagnostico_eventos(desde: int = 0, carregador_id: str | None = None, limite: int = 300):
    return diagnostico.listar(desde, carregador_id, min(limite, 1000))


@router.delete("/diagnostico/eventos", dependencies=[Depends(exigir_admin)])
def diagnostico_limpar():
    diagnostico.limpar()
    return {"ok": True}


# ---- Caixa do Mercado Pago (venda direto na maquininha) -----------------

@router.post("/carregadores/{carregador_id}/mercado-pago/caixa", dependencies=[Depends(exigir_admin)])
def criar_caixa_mp(carregador_id: str, endereco: EnderecoLoja | None = None, db: Session = Depends(get_db)):
    """Cria (ou reaproveita) a loja e o caixa do carregador no Mercado Pago.
    Depois e' so vincular a maquininha a esse caixa no app do MP."""
    carregador = _obter(db, carregador_id)
    mercado_pago_caixa.criar_loja_e_caixa(db, carregador, endereco.model_dump() if endereco else None)
    return _serializar_carregador(carregador)


@router.post("/carregadores/{carregador_id}/simular-pagamento", dependencies=[Depends(exigir_admin)])
def simular_pagamento_caixa(carregador_id: str, dados: PagamentoSimulado, db: Session = Depends(get_db)):
    """Como se a maquininha do caixa tivesse aprovado este valor: passa pela
    mesma regra de preco e liberacao, sem chamar o Mercado Pago."""
    carregador = _obter(db, carregador_id)
    if not mercado_pago_caixa.minutos_por_valor(carregador, dados.valor):
        raise HTTPException(status_code=422, detail="Configure a regra de preco do carregador antes (ex.: R$ 10 = 20 min)")
    cobranca = mercado_pago_caixa.registrar_pagamento(
        db, carregador, dados.valor, f"sim-{uuid4().hex[:10]}", "simulado", origem="simulado"
    )
    db.refresh(cobranca)
    ativa = sessoes.sessao_ativa(db, carregador_id)
    return {
        "minutos": cobranca.minutos,
        "valor": cobranca.valor,
        "estornada": bool(cobranca.estornada_em),
        "sessao": serializar_sessao(ativa) if ativa else None,
    }


# ---- Clientes (donos dos carregadores, com a conta do Mercado Pago) -----

def _serializar_cliente(cliente: Cliente, db: Session) -> dict:
    return {
        "id": cliente.id,
        "nome_empresa": cliente.nome_empresa,
        "email_contato": cliente.email_contato,
        "telefone": cliente.telefone,
        "documento": cliente.documento,
        "endereco_rua": cliente.endereco_rua,
        "endereco_numero": cliente.endereco_numero,
        "endereco_cidade": cliente.endereco_cidade,
        "endereco_estado": cliente.endereco_estado,
        "endereco_latitude": cliente.endereco_latitude,
        "endereco_longitude": cliente.endereco_longitude,
        "ativo": cliente.ativo,
        "tem_token_mp": bool(cliente.mp_access_token),
        # So o comeco, para conferir qual conta esta ligada sem expor o token.
        "token_mp_prefixo": (cliente.mp_access_token or "")[:12] or None,
        "mp_user_id": cliente.mp_user_id,
        "carregadores": db.query(Carregador).filter(Carregador.cliente_id == cliente.id).count(),
        "created_at": cliente.created_at,
    }


def _aplicar_dados_cliente(cliente: Cliente, dados: ClienteDados) -> None:
    campos = dados.model_dump(exclude_unset=True)
    token = (campos.pop("mp_access_token", None) or "").strip()
    for campo, valor in campos.items():
        setattr(cliente, campo, valor.strip() if isinstance(valor, str) else valor)
    if token:
        # Confere o token no MP antes de salvar (token errado so apareceria
        # na hora de criar o caixa ou, pior, de receber um pagamento).
        try:
            cliente.mp_user_id = pagamentos.mp_user_id_do_token(token)
        except HTTPException as exc:
            raise HTTPException(status_code=422, detail=f"Token do Mercado Pago recusado: {exc.detail}") from exc
        cliente.mp_access_token = token


@router.get("/clientes", dependencies=[Depends(exigir_admin)])
def listar_clientes(db: Session = Depends(get_db)):
    return [_serializar_cliente(c, db) for c in db.query(Cliente).order_by(Cliente.nome_empresa).all()]


@router.post("/clientes", dependencies=[Depends(exigir_admin)])
def criar_cliente(dados: ClienteDados, db: Session = Depends(get_db)):
    if not (dados.nome_empresa or "").strip():
        raise HTTPException(status_code=422, detail="Informe o nome do cliente")
    cliente = Cliente(nome_empresa=dados.nome_empresa.strip())
    _aplicar_dados_cliente(cliente, dados)
    db.add(cliente)
    db.commit()
    return _serializar_cliente(cliente, db)


@router.patch("/clientes/{cliente_id}", dependencies=[Depends(exigir_admin)])
def atualizar_cliente(cliente_id: int, dados: ClienteDados, db: Session = Depends(get_db)):
    cliente = db.get(Cliente, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    _aplicar_dados_cliente(cliente, dados)
    db.commit()
    return _serializar_cliente(cliente, db)
