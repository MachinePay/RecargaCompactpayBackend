"""Administracao (header X-Admin-Key = ADMIN_API_KEY): cadastro de
carregador, pacotes e tablets, sessoes e comandos OCPP de diagnostico.
Fica assim ate o painel ser integrado ao frontend da CompactPay."""
import concurrent.futures
import hmac
import socket
from datetime import date, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, exists, func, not_, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.carregador import Carregador, Cliente, Cobranca, HistoricoOperacao, PacoteTempo, SessaoCarga, TerminalTablet, Usuario, agora
from app.services import auth, diagnostico, geo, trava, mercado_pago_caixa, ocpp_server, pagamentos, relatorios, saude, sessoes, sumup, terminais
from app.services.ocpp_handlers import STATUS_SESSAO_ATIVA, energia_kwh, registrar_historico
from app.api.v1.endpoints.tablet import serializar_sessao

router = APIRouter(prefix="/admin", tags=["admin"])

OCPP_RESPOSTA_TIMEOUT_SECONDS = 15


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class Acesso(BaseModel):
    """Quem esta usando o painel: admin (chave ou usuario admin) ou cliente."""
    role: str
    cliente_id: int | None = None
    usuario_id: int | None = None
    nome: str = "Administrador"

    @property
    def admin(self) -> bool:
        return self.role == "admin"


def acesso_atual(
    x_admin_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Acesso:
    if x_admin_key:
        if settings.ADMIN_API_KEY and hmac.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
            return Acesso(role="admin")
        raise HTTPException(status_code=401, detail="Chave de admin invalida")
    if authorization and authorization.lower().startswith("bearer "):
        dados = auth.ler_token(authorization[7:].strip())
        usuario = db.get(Usuario, int(dados.get("sub") or 0))
        if usuario is None or not usuario.ativo:
            raise HTTPException(status_code=401, detail="Usuario desativado")
        if usuario.role != "admin" and not usuario.cliente_id:
            raise HTTPException(status_code=403, detail="Usuario sem cliente vinculado")
        return Acesso(role=usuario.role, cliente_id=usuario.cliente_id, usuario_id=usuario.id, nome=usuario.nome)
    raise HTTPException(status_code=401, detail="Entre no painel")


def exigir_admin(acesso: Acesso = Depends(acesso_atual)) -> Acesso:
    if not acesso.admin:
        raise HTTPException(status_code=403, detail="So o administrador pode fazer isso")
    return acesso


def _carregadores_visiveis(db: Session, acesso: Acesso):
    query = db.query(Carregador).filter(Carregador.excluido_em.is_(None))
    return query if acesso.admin else query.filter(Carregador.cliente_id == acesso.cliente_id)


def _checar_acesso(db: Session, acesso: Acesso, carregador_id: str) -> Carregador:
    carregador = db.get(Carregador, carregador_id)
    if carregador is None or carregador.excluido_em is not None or (not acesso.admin and carregador.cliente_id != acesso.cliente_id):
        raise HTTPException(status_code=404, detail="Carregador nao encontrado")
    return carregador


BANCOS = ("mercado_pago", "sumup")


class CarregadorCreate(BaseModel):
    """Igual o cadastro de maquina da CompactPay: nasce dentro de um cliente,
    com o banco da maquininha escolhido entre os que o cliente usa."""
    id_hardware: str = Field(min_length=1, max_length=48, pattern=r"^[A-Za-z0-9_\-.]+$")
    nome_local: str = Field(min_length=1, max_length=80)
    cliente_id: int
    banco_pagamento: str = "mercado_pago"
    connector_id: int = 1
    # SumUp: reader pareado ou serial da maquininha; operador para o Pix.
    sumup_reader_id: str | None = None
    sumup_device_code: str | None = None
    sumup_usuario: str | None = None
    # Opcional: CEP (e numero) para ja aparecer no mapa.
    cep: str | None = None
    numero_endereco: str | None = None
    mp_terminal_id: str | None = None
    mp_access_token: str | None = None
    gerar_senha_ocpp: bool = False


class CarregadorUpdate(BaseModel):
    nome_local: str | None = None
    connector_id: int | None = None
    mp_terminal_id: str | None = None
    mp_access_token: str | None = None
    ativo: bool | None = None
    cliente_id: int | None = None
    sumup_reader_id: str | None = None
    sumup_device_code: str | None = None
    sumup_usuario: str | None = None
    cep: str | None = None
    endereco: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
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
    cliente_mercado_pago: bool | None = None
    cliente_sumup: bool | None = None
    # Vazio na edicao = manter o que esta salvo.
    mp_access_token: str | None = None
    mp_public_key: str | None = None
    mp_client_id: str | None = None
    mp_client_secret: str | None = None  # vazio na edicao = manter
    sumup_api_key: str | None = None
    ativo: bool | None = None
    # Login do cliente no painel (cria/atualiza um Usuario role cliente).
    acesso_email: str | None = None
    acesso_senha: str | None = None


class ParearSumup(BaseModel):
    pairing_code: str = Field(min_length=4, max_length=20)
    nome: str | None = None


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


def _inicio_do_dia():
    """Meia-noite de hoje em Brasilia (UTC-3), em UTC sem fuso."""
    return (agora() - timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=3)


QUEDA = "Carregador desconectou do servidor OCPP"


def _indicadores_hoje(db: Session, ids: list[str] | None = None) -> dict[str, dict]:
    """Vendas, energia e quedas de hoje por carregador (uma consulta por tipo).
    ids: so esses carregadores (a pagina aberta da lista)."""
    inicio = _inicio_do_dia()
    por_carregador: dict[str, dict] = {}

    def item(carregador_id: str) -> dict:
        return por_carregador.setdefault(
            carregador_id, {"faturamento_hoje": 0.0, "vendas_hoje": 0, "recargas_hoje": 0, "energia_hoje_kwh": 0.0, "quedas_hoje": 0}
        )

    vendas = (
        db.query(Cobranca)
        .filter(
            Cobranca.status == "aprovada",
            Cobranca.pago_em >= inicio,
            Cobranca.estornada_em.is_(None),
            Cobranca.origem.in_(("point", "simulado", "caixa", "sumup", "pix")),
            *([Cobranca.carregador_id.in_(ids)] if ids is not None else []),
        )
        .all()
    )
    for venda in vendas:
        dados = item(venda.carregador_id)
        dados["faturamento_hoje"] = round(dados["faturamento_hoje"] + venda.valor, 2)
        dados["vendas_hoje"] += 1
    sessoes_hoje = db.query(SessaoCarga).filter(SessaoCarga.inicio >= inicio)
    if ids is not None:
        sessoes_hoje = sessoes_hoje.filter(SessaoCarga.carregador_id.in_(ids))
    for sessao in sessoes_hoje.all():
        dados = item(sessao.carregador_id)
        dados["recargas_hoje"] += 1
        dados["energia_hoje_kwh"] = round(dados["energia_hoje_kwh"] + energia_kwh(sessao), 3)
    quedas = (
        db.query(HistoricoOperacao)
        .filter(
            HistoricoOperacao.created_at >= inicio,
            HistoricoOperacao.descricao == QUEDA,
            *([HistoricoOperacao.carregador_id.in_(ids)] if ids is not None else []),
        )
        .all()
    )
    for queda in quedas:
        item(queda.carregador_id)["quedas_hoje"] += 1
    return por_carregador


def _indicadores_vazios() -> dict:
    return {"faturamento_hoje": 0.0, "vendas_hoje": 0, "recargas_hoje": 0, "energia_hoje_kwh": 0.0, "quedas_hoje": 0}


def _serializar_carregador(carregador: Carregador) -> dict:
    conexao = ocpp_server.info_conexao(carregador.id_hardware)
    return {
        "id_hardware": carregador.id_hardware,
        "nome_local": carregador.nome_local,
        "connector_id": carregador.connector_id,
        "mp_terminal_id": carregador.mp_terminal_id,
        "ativo": carregador.ativo,
        "online": conexao is not None,
        "travado": trava.travado(carregador.id_hardware) if conexao is not None else None,
        "online_desde": conexao["desde"] if conexao else None,
        "ip": conexao["ip"] if conexao else None,
        "corrente_maxima_a": carregador.corrente_maxima_a,
        "cep": carregador.cep,
        "endereco": carregador.endereco,
        "latitude": carregador.latitude,
        "longitude": carregador.longitude,
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
        "banco_pagamento": carregador.banco_pagamento or "mercado_pago",
        "sumup_reader_id": carregador.sumup_reader_id,
        "sumup_device_code": carregador.sumup_device_code,
        "sumup_usuario": carregador.sumup_usuario,
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
    existente = db.get(Carregador, dados.id_hardware)
    if existente is not None and existente.excluido_em is None:
        raise HTTPException(status_code=409, detail="Carregador ja cadastrado")
    cliente = db.get(Cliente, dados.cliente_id)
    if cliente is None:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    if dados.banco_pagamento not in BANCOS:
        raise HTTPException(status_code=422, detail="Banco invalido: use mercado_pago ou sumup")
    if dados.banco_pagamento not in _bancos_do_cliente(cliente):
        raise HTTPException(status_code=422, detail="Este cliente nao esta configurado para esse banco (veja Clientes)")
    if dados.banco_pagamento == "sumup" and not (dados.sumup_reader_id or dados.sumup_device_code):
        raise HTTPException(status_code=422, detail="Escolha a maquininha SumUp (pareada ou pelo serial)")
    campos = dict(
        nome_local=dados.nome_local,
        connector_id=dados.connector_id,
        mp_terminal_id=dados.mp_terminal_id,
        mp_access_token=dados.mp_access_token,
        cliente_id=dados.cliente_id,
        banco_pagamento=dados.banco_pagamento,
        sumup_reader_id=(dados.sumup_reader_id or "").strip() or None,
        sumup_device_code=(dados.sumup_device_code or "").strip() or None,
        sumup_usuario=(dados.sumup_usuario or "").strip() or None,
    )
    if existente is not None:
        # ID de um carregador excluido (que tinha vendas): volta a valer como
        # cadastro novo, sem a configuracao antiga; o historico continua.
        carregador = existente
        for campo in ("mp_user_id", "mp_store_id", "mp_store_external_id", "mp_pos_id", "mp_pos_external_id",
                      "preco_valor", "preco_minutos", "cep", "endereco", "latitude", "longitude", "ocpp_senha_hash"):
            setattr(carregador, campo, None)
        for campo, valor in campos.items():
            setattr(carregador, campo, valor)
        carregador.excluido_em = None
        carregador.ativo = True
    else:
        carregador = Carregador(id_hardware=dados.id_hardware, **campos)
    if (dados.cep or "").strip():
        # Opcional: endereco e ponto aproximado no mapa. Falhar aqui nao impede o cadastro.
        try:
            local = geo.buscar_cep(dados.cep, (dados.numero_endereco or "").strip() or None)
            carregador.cep = local["cep"]
            carregador.endereco = local["endereco"]
            carregador.latitude, carregador.longitude = local["latitude"], local["longitude"]
        except HTTPException:
            carregador.cep = dados.cep.strip()
    senha = None
    if dados.gerar_senha_ocpp:
        # Senha curta o bastante para digitar no app do fabricante.
        senha = terminais.gerar_chave("")[:20]
        carregador.ocpp_senha_hash = terminais.hash_chave(senha)
    if existente is None:
        db.add(carregador)
    db.commit()
    aviso_caixa = None
    if carregador.banco_pagamento == "mercado_pago":
        # Igual CompactPay: a loja e o caixa nascem junto com a maquina. Se o MP
        # recusar, o carregador fica cadastrado e da para tentar de novo na tela dele.
        try:
            mercado_pago_caixa.criar_loja_e_caixa(db, carregador)
        except HTTPException as exc:
            db.rollback()
            aviso_caixa = str(exc.detail)
    db.refresh(carregador)
    resposta = _serializar_carregador(carregador)
    resposta["senha_ocpp"] = senha  # aparece so agora
    resposta["aviso_caixa"] = aviso_caixa
    return resposta


def _busca(query, termo: str | None, *colunas):
    """Cada palavra precisa aparecer em alguma das colunas (sem diferenciar maiuscula)."""
    for palavra in (termo or "").split()[:5]:
        padrao = f"%{palavra.replace('%', '').replace('_', '')}%"
        query = query.filter(or_(*(coluna.ilike(padrao) for coluna in colunas)))
    return query


def _completar_carregadores(db: Session, lista: list[Carregador]) -> list[dict]:
    """Serializa com os numeros de hoje e a recarga ativa (consultas em lote)."""
    ids = [c.id_hardware for c in lista]
    hoje = _indicadores_hoje(db, ids)
    ativas: dict[str, SessaoCarga] = {}
    if ids:
        for sessao in (
            db.query(SessaoCarga)
            .filter(SessaoCarga.carregador_id.in_(ids), SessaoCarga.status.in_(STATUS_SESSAO_ATIVA))
            .order_by(SessaoCarga.id)
            .all()
        ):
            ativas[sessao.carregador_id] = sessao  # fica a mais nova
    resposta = []
    for carregador in lista:
        item = _serializar_carregador(carregador)
        item.update(hoje.get(carregador.id_hardware) or _indicadores_vazios())
        ativa = ativas.get(carregador.id_hardware)
        item["sessao_ativa"] = serializar_sessao(ativa) if ativa else None
        resposta.append(item)
    return resposta


def _condicoes_status() -> dict:
    """Status ao vivo como condicao SQL: online vem da memoria (conexoes abertas)."""
    online = Carregador.id_hardware.in_(ocpp_server.conectados())
    defeito = or_(and_(Carregador.erro_conector.isnot(None), Carregador.erro_conector != ""), Carregador.status_conector == "Faulted")
    carregando = exists().where(SessaoCarga.carregador_id == Carregador.id_hardware, SessaoCarga.status.in_(STATUS_SESSAO_ATIVA))
    return {
        "offline": not_(online),
        "defeito": and_(online, defeito),
        "carregando": and_(online, not_(defeito), carregando),
        "online": and_(online, not_(defeito), not_(carregando)),
    }


def _ordem_carregadores(coluna: str | None, direcao: str, condicoes: dict):
    inicio = _inicio_do_dia()
    if coluna == "carregador":
        valor = func.lower(Carregador.nome_local)
    elif coluna == "cliente":
        valor = select(func.lower(Cliente.nome_empresa)).where(Cliente.id == Carregador.cliente_id).scalar_subquery()
    elif coluna == "hoje":
        valor = (
            select(func.coalesce(func.sum(Cobranca.valor), 0))
            .where(
                Cobranca.carregador_id == Carregador.id_hardware,
                Cobranca.status == "aprovada",
                Cobranca.pago_em >= inicio,
                Cobranca.estornada_em.is_(None),
            )
            .scalar_subquery()
        )
    elif coluna == "conexao":
        # Menos quedas = melhor; offline por ultimo.
        quedas = (
            select(func.count(HistoricoOperacao.id))
            .where(HistoricoOperacao.carregador_id == Carregador.id_hardware, HistoricoOperacao.created_at >= inicio, HistoricoOperacao.descricao == QUEDA)
            .scalar_subquery()
        )
        valor = case((condicoes["offline"], -1), else_=1000 - quedas)
    else:  # status: problema primeiro (igual a CompactPay)
        valor = case((condicoes["offline"], 0), (condicoes["defeito"], 1), (condicoes["carregando"], 2), else_=3)
    return [valor.desc() if direcao == "desc" else valor.asc(), Carregador.nome_local.asc()]


@router.get("/carregadores")
def listar_carregadores(
    pagina: int | None = None,
    por_pagina: int = 25,
    busca: str | None = None,
    cliente_id: int | None = None,
    status: str | None = None,
    ordem: str | None = None,
    direcao: str = "asc",
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    """Com pagina: lista paginada com busca, filtro e ordem no servidor, mais a
    contagem por status e o resumo do dia. Sem pagina: todos (mapa e painel ao
    vivo; para os selects use /carregadores/opcoes)."""
    if pagina is None:
        return _completar_carregadores(db, _carregadores_visiveis(db, acesso).order_by(Carregador.id_hardware).all())
    condicoes = _condicoes_status()
    visiveis = _carregadores_visiveis(db, acesso)
    query = visiveis
    if cliente_id:
        query = query.filter(Carregador.cliente_id == cliente_id)
    query = _busca(
        query.outerjoin(Cliente, Cliente.id == Carregador.cliente_id),
        busca,
        Carregador.id_hardware,
        Carregador.nome_local,
        Carregador.endereco,
        Carregador.modelo,
        Carregador.vendor,
        Carregador.firmware_version,
        Cliente.nome_empresa,
    )
    contagem = {chave: query.filter(condicao).count() for chave, condicao in condicoes.items()}
    if status:
        if status not in condicoes:
            raise HTTPException(status_code=422, detail="Status invalido")
        query = query.filter(condicoes[status])
    resultado = _paginar(query, pagina, por_pagina, None)
    resultado["itens"] = query.order_by(*_ordem_carregadores(ordem, direcao, condicoes)).offset(
        (resultado["pagina"] - 1) * resultado["por_pagina"]
    ).limit(resultado["por_pagina"]).all()
    resultado["itens"] = _completar_carregadores(db, resultado["itens"])
    resultado["contagem"] = {"todos": sum(contagem.values()), **contagem}
    # Cartoes do topo: todos os carregadores visiveis, sem a busca.
    ids_visiveis = select(Carregador.id_hardware).where(
        Carregador.excluido_em.is_(None), *([] if acesso.admin else [Carregador.cliente_id == acesso.cliente_id])
    )
    vendas = (
        db.query(func.coalesce(func.sum(Cobranca.valor), 0), func.count(Cobranca.id))
        .filter(
            Cobranca.carregador_id.in_(ids_visiveis),
            Cobranca.status == "aprovada",
            Cobranca.pago_em >= _inicio_do_dia(),
            Cobranca.estornada_em.is_(None),
            Cobranca.origem.in_(("point", "simulado", "caixa", "sumup", "pix")),
        )
        .one()
    )
    resultado["resumo"] = {
        "cadastrados": visiveis.count(),
        "online": visiveis.filter(Carregador.id_hardware.in_(ocpp_server.conectados())).count(),
        "carregando": visiveis.filter(condicoes["carregando"]).count(),
        "faturamento_hoje": round(float(vendas[0] or 0), 2),
        "vendas_hoje": vendas[1],
    }
    return resultado


@router.get("/carregadores/opcoes")
def opcoes_carregadores(cliente_id: int | None = None, db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    """So id e nome, para os selects (filtros e formularios)."""
    query = _carregadores_visiveis(db, acesso)
    if cliente_id:
        query = query.filter(Carregador.cliente_id == cliente_id)
    return [
        {"id_hardware": c.id_hardware, "nome_local": c.nome_local, "cliente_id": c.cliente_id, "cliente_nome": c.cliente.nome_empresa if c.cliente else None}
        for c in query.order_by(Carregador.nome_local).all()
    ]


@router.get("/carregadores/{carregador_id}")
def obter_carregador(carregador_id: str, db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    carregador = _checar_acesso(db, acesso, carregador_id)
    item = _serializar_carregador(carregador)
    item.update(_indicadores_hoje(db).get(carregador_id) or _indicadores_vazios())
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


@router.get("/resumo")
def resumo(db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    """Numeros do dashboard. "Hoje" e' o dia em UTC-3 (Brasilia)."""
    inicio_dia = (agora() - timedelta(hours=3)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=3)
    carregadores = _carregadores_visiveis(db, acesso).filter(Carregador.ativo.is_(True)).all()
    ids = [c.id_hardware for c in _carregadores_visiveis(db, acesso).all()]
    vendas = (
        db.query(Cobranca)
        .filter(
            Cobranca.status == "aprovada",
            Cobranca.pago_em >= inicio_dia,
            Cobranca.estornada_em.is_(None),
            Cobranca.origem.in_(("point", "simulado", "caixa", "sumup", "pix")),
            Cobranca.carregador_id.in_(ids),
        )
        .all()
    )
    sessoes_hoje = db.query(SessaoCarga).filter(SessaoCarga.inicio >= inicio_dia, SessaoCarga.carregador_id.in_(ids)).all()
    return {
        "carregadores_total": len(carregadores),
        "carregadores_online": sum(1 for c in carregadores if ocpp_server.esta_conectado(c.id_hardware)),
        "sessoes_ativas": db.query(SessaoCarga).filter(SessaoCarga.status.in_(STATUS_SESSAO_ATIVA), SessaoCarga.carregador_id.in_(ids)).count(),
        "faturamento_hoje": round(sum(v.valor for v in vendas), 2),
        "vendas_hoje": len(vendas),
        "energia_hoje_kwh": round(sum(energia_kwh(s) for s in sessoes_hoje), 3),
        "estornos_pendentes": db.query(Cobranca)
        .filter(Cobranca.estorno_erro.isnot(None), Cobranca.estornada_em.is_(None), Cobranca.carregador_id.in_(ids))
        .count(),
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
    renomeou = "nome_local" in campos and campos["nome_local"] != carregador.nome_local
    for campo, valor in campos.items():
        setattr(carregador, campo, valor)
    db.commit()
    db.refresh(carregador)
    resposta = _serializar_carregador(carregador)
    if renomeou and carregador.mp_pos_id:
        # Igual o nome no app do Mercado Pago, para achar a loja/caixa certos.
        try:
            mercado_pago_caixa.renomear_no_mp(carregador)
        except HTTPException as exc:
            resposta["aviso_mp"] = f"Nome salvo aqui, mas o Mercado Pago recusou renomear: {exc.detail}"
    return resposta


# Listas que crescem sem parar (recargas, historico) sao sempre paginadas.
POR_PAGINA_MAX = 100


def _paginar(query, pagina: int, por_pagina: int, ordem) -> dict:
    por_pagina = max(1, min(por_pagina, POR_PAGINA_MAX))
    pagina = max(1, pagina)
    total = query.order_by(None).count()
    itens = query.order_by(ordem).offset((pagina - 1) * por_pagina).limit(por_pagina).all() if ordem is not None else []
    return {"itens": itens, "total": total, "pagina": pagina, "por_pagina": por_pagina, "paginas": max(1, -(-total // por_pagina))}


def _periodo_utc(de: date | None, ate: date | None) -> tuple[datetime | None, datetime | None]:
    """Dias de Brasilia (fim incluso) -> limites em UTC."""
    return (relatorios._utc(de) if de else None, relatorios._utc(ate + timedelta(days=1)) if ate else None)


@router.get("/carregadores/{carregador_id}/historico")
def historico_carregador(
    carregador_id: str,
    categoria: str | None = None,
    pagina: int = 1,
    por_pagina: int = 50,
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    _checar_acesso(db, acesso, carregador_id)
    query = db.query(HistoricoOperacao).filter(HistoricoOperacao.carregador_id == carregador_id)
    if categoria:
        query = query.filter(HistoricoOperacao.categoria == categoria)
    resultado = _paginar(query, pagina, por_pagina, HistoricoOperacao.id.desc())
    resultado["itens"] = [
        {"categoria": h.categoria, "descricao": h.descricao, "valor": h.valor, "created_at": h.created_at} for h in resultado["itens"]
    ]
    return resultado


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


def _sessoes_visiveis(db: Session, acesso: Acesso):
    query = db.query(SessaoCarga)
    if not acesso.admin:
        # Subconsulta: nao traz a lista de carregadores do cliente para a memoria.
        meus = select(Carregador.id_hardware).where(Carregador.cliente_id == acesso.cliente_id)
        query = query.filter(SessaoCarga.carregador_id.in_(meus))
    return query


# Filtro de status das telas de recargas.
GRUPOS_STATUS = {
    "ativas": ("aguardando_inicio", "aguardando_carro", "carregando", "encerrando"),
    "finalizada": ("finalizada",),
    "falhou": ("falhou",),
}


@router.get("/recargas")
def listar_recargas(
    carregador_id: str | None = None,
    status: str | None = None,
    origem: str | None = None,
    de: date | None = None,
    ate: date | None = None,
    estornadas: bool = False,
    pagina: int = 1,
    por_pagina: int = 25,
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    """Recargas paginadas (mais nova primeiro) com filtros. Periodo em dias de Brasilia."""
    query = _sessoes_visiveis(db, acesso)
    if carregador_id:
        query = query.filter(SessaoCarga.carregador_id == carregador_id)
    if status:
        if status not in GRUPOS_STATUS:
            raise HTTPException(status_code=422, detail="Status invalido")
        query = query.filter(SessaoCarga.status.in_(GRUPOS_STATUS[status]))
    inicio, fim = _periodo_utc(de, ate)
    if inicio:
        query = query.filter(SessaoCarga.created_at >= inicio)
    if fim:
        query = query.filter(SessaoCarga.created_at < fim)
    if origem or estornadas:
        query = query.join(Cobranca, Cobranca.id == SessaoCarga.cobranca_id)
        if origem:
            query = query.filter(Cobranca.origem == origem)
        if estornadas:
            query = query.filter(Cobranca.estornada_em.isnot(None))
    resultado = _paginar(query, pagina, por_pagina, SessaoCarga.id.desc())
    resultado["itens"] = _serializar_sessoes(db, resultado["itens"])
    return resultado


@router.get("/sessoes")
def listar_sessoes(carregador_id: str | None = None, limite: int = 50, db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    """Ultimas N (telas ao vivo). Para consultar o passado: /recargas."""
    query = _sessoes_visiveis(db, acesso)
    if carregador_id:
        query = query.filter(SessaoCarga.carregador_id == carregador_id)
    return _serializar_sessoes(db, query.order_by(SessaoCarga.id.desc()).limit(min(limite, POR_PAGINA_MAX)).all())


def _serializar_sessoes(db: Session, lista: list[SessaoCarga]) -> list[dict]:
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

def _bancos_do_cliente(cliente: Cliente) -> list[str]:
    bancos = []
    if cliente.cliente_mercado_pago or (cliente.cliente_mercado_pago is None and cliente.mp_access_token):
        bancos.append("mercado_pago")
    if cliente.cliente_sumup:
        bancos.append("sumup")
    return bancos


def _serializar_cliente(cliente: Cliente, db: Session) -> dict:
    return {
        "bancos": _bancos_do_cliente(cliente),
        "cliente_mercado_pago": "mercado_pago" in _bancos_do_cliente(cliente),
        "cliente_sumup": bool(cliente.cliente_sumup),
        "tem_sumup": bool(cliente.sumup_api_key and cliente.sumup_merchant_code),
        "sumup_merchant_code": cliente.sumup_merchant_code,
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
        # Public Key e Client ID nao sao segredo; o Client Secret nunca volta.
        "mp_public_key": cliente.mp_public_key,
        "mp_client_id": cliente.mp_client_id,
        "tem_client_secret": bool(cliente.mp_client_secret),
        "mp_user_id": cliente.mp_user_id,
        "carregadores": db.query(Carregador).filter(Carregador.cliente_id == cliente.id).count(),
        "acessos": [u.email for u in db.query(Usuario).filter(Usuario.cliente_id == cliente.id).order_by(Usuario.id).all()],
        "created_at": cliente.created_at,
    }


def _aplicar_dados_cliente(cliente: Cliente, dados: ClienteDados) -> None:
    campos = dados.model_dump(exclude_unset=True, exclude={"acesso_email", "acesso_senha"})
    token = (campos.pop("mp_access_token", None) or "").strip()
    chave_sumup = (campos.pop("sumup_api_key", None) or "").strip()
    segredo = (campos.pop("mp_client_secret", None) or "").strip()
    if segredo:
        cliente.mp_client_secret = segredo
    if chave_sumup:
        # Confere a chave e ja descobre o merchant_code (GET /v0.1/me).
        try:
            cliente.sumup_merchant_code = sumup.merchant_code(chave_sumup)
        except HTTPException as exc:
            raise HTTPException(status_code=422, detail=f"API key da SumUp recusada: {exc.detail}") from exc
        cliente.sumup_api_key = chave_sumup
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


@router.get("/clientes")
def listar_clientes(
    pagina: int | None = None,
    por_pagina: int = 25,
    busca: str | None = None,
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    """Com pagina: paginada com busca. Sem pagina: todos (selects: /clientes/opcoes)."""
    query = db.query(Cliente)
    if not acesso.admin:
        query = query.filter(Cliente.id == acesso.cliente_id)
    if pagina is None:
        return [_serializar_cliente(c, db) for c in query.order_by(Cliente.nome_empresa).all()]
    query = _busca(query, busca, Cliente.nome_empresa, Cliente.email_contato, Cliente.telefone, Cliente.documento)
    resultado = _paginar(query, pagina, por_pagina, Cliente.nome_empresa.asc())
    resultado["itens"] = [_serializar_cliente(c, db) for c in resultado["itens"]]
    return resultado


@router.get("/clientes/opcoes")
def opcoes_clientes(sem_login: bool = False, db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    """Leve, para os selects: nome, bancos e se tem credencial."""
    query = db.query(Cliente)
    if not acesso.admin:
        query = query.filter(Cliente.id == acesso.cliente_id)
    if sem_login:
        query = query.filter(~exists().where(Usuario.cliente_id == Cliente.id))
    return [
        {
            "id": c.id,
            "nome_empresa": c.nome_empresa,
            "email_contato": c.email_contato,
            "bancos": _bancos_do_cliente(c),
            "tem_token_mp": bool(c.mp_access_token),
            "tem_sumup": bool(c.sumup_api_key and c.sumup_merchant_code),
        }
        for c in query.order_by(Cliente.nome_empresa).limit(500 if not sem_login else 20).all()
    ]


@router.post("/clientes", dependencies=[Depends(exigir_admin)])
def criar_cliente(dados: ClienteDados, db: Session = Depends(get_db)):
    if not (dados.nome_empresa or "").strip():
        raise HTTPException(status_code=422, detail="Informe o nome do cliente")
    cliente = Cliente(nome_empresa=dados.nome_empresa.strip())
    _aplicar_dados_cliente(cliente, dados)
    db.add(cliente)
    db.flush()
    _criar_acesso_cliente(db, cliente, dados)
    db.commit()
    return _serializar_cliente(cliente, db)


def _criar_acesso_cliente(db: Session, cliente: Cliente, dados: ClienteDados) -> None:
    """Igual a CompactPay: cadastrar o cliente ja cria o login dele no painel.
    E-mail ja existente desse cliente = troca a senha."""
    email = (dados.acesso_email or "").strip().lower()
    senha = dados.acesso_senha or ""
    if not email and not senha:
        return
    if not email or len(senha) < 6:
        raise HTTPException(status_code=422, detail="Login do cliente: informe e-mail e senha (minimo 6 caracteres)")
    usuario = db.query(Usuario).filter(Usuario.email == email).first()
    if usuario is not None and usuario.cliente_id != cliente.id:
        raise HTTPException(status_code=409, detail="Ja existe um usuario com esse e-mail")
    if usuario is None:
        usuario = Usuario(nome=cliente.nome_empresa, email=email, role="cliente", cliente_id=cliente.id)
        db.add(usuario)
    usuario.senha_hash = auth.hash_senha(senha)
    usuario.ativo = True


@router.patch("/clientes/{cliente_id}", dependencies=[Depends(exigir_admin)])
def atualizar_cliente(cliente_id: int, dados: ClienteDados, db: Session = Depends(get_db)):
    cliente = db.get(Cliente, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    _aplicar_dados_cliente(cliente, dados)
    _criar_acesso_cliente(db, cliente, dados)
    db.commit()
    return _serializar_cliente(cliente, db)


# ---- Gestao (relatorio por periodo) --------------------------------------

@router.get("/relatorio")
def relatorio(
    periodo: str = "7d",
    inicio: date | None = None,
    fim: date | None = None,
    cliente_id: int | None = None,
    carregador_id: str | None = None,
    incluir_simulados: bool = False,
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    """Faturamento, graficos por dia e por hora, ranking e formas de pagamento.
    periodo: hoje, 7d, 30d, mes, mes_anterior ou personalizado (com inicio e fim)."""
    if not acesso.admin:
        cliente_id = acesso.cliente_id  # cliente so ve o proprio relatorio
    return relatorios.gerar(db, periodo, inicio, fim, cliente_id, carregador_id, incluir_simulados)



def _cliente_sumup(db: Session, cliente_id: int) -> Cliente:
    cliente = db.get(Cliente, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    if not (cliente.sumup_api_key and cliente.sumup_merchant_code):
        raise HTTPException(status_code=422, detail="Cliente sem API key da SumUp")
    return cliente


@router.get("/clientes/{cliente_id}/sumup/readers", dependencies=[Depends(exigir_admin)])
def listar_maquininhas_sumup(cliente_id: int, db: Session = Depends(get_db)):
    cliente = _cliente_sumup(db, cliente_id)
    return sumup.listar_readers(cliente.sumup_api_key, cliente.sumup_merchant_code)


@router.post("/clientes/{cliente_id}/sumup/readers", dependencies=[Depends(exigir_admin)])
def parear_maquininha_sumup(cliente_id: int, dados: ParearSumup, db: Session = Depends(get_db)):
    """Codigo que aparece na maquininha (Conexoes > Wi-Fi > API > Conectar), vale 5 min."""
    cliente = _cliente_sumup(db, cliente_id)
    return sumup.parear_reader(cliente.sumup_api_key, cliente.sumup_merchant_code, dados.pairing_code, dados.nome)


# ---- Quedas e saude ----------------------------------------------------

@router.get("/quedas")
def historico_quedas(
    periodo: str = "7d",
    inicio: date | None = None,
    fim: date | None = None,
    carregador_id: str | None = None,
    cliente_id: int | None = None,
    db: Session = Depends(get_db),
    acesso: Acesso = Depends(acesso_atual),
):
    dia_ini, dia_fim = relatorios.intervalo(periodo, inicio, fim)
    query = _carregadores_visiveis(db, acesso)
    if cliente_id and acesso.admin:
        query = query.filter(Carregador.cliente_id == cliente_id)
    if carregador_id:
        query = query.filter(Carregador.id_hardware == carregador_id)
    carregadores = query.all()
    lista = saude.quedas(db, carregadores, relatorios._utc(dia_ini), relatorios._utc(dia_fim + timedelta(days=1)))
    return {
        "periodo": {"inicio": dia_ini.isoformat(), "fim": dia_fim.isoformat()},
        "quedas": lista,
        "por_carregador": saude.resumo_quedas(lista, carregadores),
    }


@router.get("/saude")
def saude_carregadores(db: Session = Depends(get_db), acesso: Acesso = Depends(acesso_atual)):
    return saude.saude(db, _carregadores_visiveis(db, acesso).all())


# ---- Usuarios (login do painel) -----------------------------------------

class UsuarioDados(BaseModel):
    nome: str | None = Field(default=None, min_length=1, max_length=80)
    email: str | None = Field(default=None, min_length=3, max_length=120)
    senha: str | None = Field(default=None, min_length=6, max_length=120)
    role: str | None = None
    cliente_id: int | None = None
    ativo: bool | None = None


def _serializar_usuario(u: Usuario) -> dict:
    return {
        "id": u.id,
        "nome": u.nome,
        "email": u.email,
        "role": u.role,
        "cliente_id": u.cliente_id,
        "cliente_nome": u.cliente.nome_empresa if u.cliente else None,
        "ativo": u.ativo,
        "ultimo_login": u.ultimo_login,
        "created_at": u.created_at,
    }


def _aplicar_usuario(db: Session, usuario: Usuario, dados: UsuarioDados) -> None:
    campos = dados.model_dump(exclude_unset=True)
    if "email" in campos:
        email = campos.pop("email").strip().lower()
        outro = db.query(Usuario).filter(Usuario.email == email, Usuario.id != usuario.id).first()
        if outro:
            raise HTTPException(status_code=409, detail="Ja existe um usuario com esse e-mail")
        usuario.email = email
    if campos.get("senha"):
        usuario.senha_hash = auth.hash_senha(campos.pop("senha"))
    campos.pop("senha", None)
    if "role" in campos and campos["role"] not in auth.PAPEIS:
        raise HTTPException(status_code=422, detail="Papel invalido: admin ou cliente")
    for campo, valor in campos.items():
        setattr(usuario, campo, valor.strip() if isinstance(valor, str) else valor)
    if usuario.role == "cliente" and (not usuario.cliente_id or db.get(Cliente, usuario.cliente_id) is None):
        raise HTTPException(status_code=422, detail="Usuario de cliente precisa de um cliente vinculado")
    if usuario.role == "admin":
        usuario.cliente_id = None


@router.get("/usuarios", dependencies=[Depends(exigir_admin)])
def listar_usuarios(
    pagina: int = 1,
    por_pagina: int = 25,
    busca: str | None = None,
    role: str | None = None,
    db: Session = Depends(get_db),
):
    query = _busca(db.query(Usuario), busca, Usuario.nome, Usuario.email)
    if role:
        query = query.filter(Usuario.role == role)
    resultado = _paginar(query, pagina, por_pagina, Usuario.nome.asc())
    resultado["itens"] = [_serializar_usuario(u) for u in resultado["itens"]]
    return resultado


@router.post("/usuarios", dependencies=[Depends(exigir_admin)])
def criar_usuario(dados: UsuarioDados, db: Session = Depends(get_db)):
    if not (dados.nome and dados.email and dados.senha):
        raise HTTPException(status_code=422, detail="Informe nome, e-mail e senha")
    usuario = Usuario(nome=dados.nome, email="", senha_hash="", role=dados.role or "cliente")
    if usuario.role == "cliente" and not dados.cliente_id:
        # Igual a CompactPay: o usuario cliente E' o dono dos carregadores, entao
        # cria o Cliente na hora (bancos e credenciais se configuram em Clientes).
        cliente = Cliente(nome_empresa=dados.nome.strip(), email_contato=dados.email.strip().lower(), cliente_mercado_pago=True)
        db.add(cliente)
        db.flush()
        dados = dados.model_copy(update={"cliente_id": cliente.id})
    _aplicar_usuario(db, usuario, dados)
    db.add(usuario)
    db.commit()
    return _serializar_usuario(usuario)


@router.patch("/usuarios/{usuario_id}", dependencies=[Depends(exigir_admin)])
def atualizar_usuario(usuario_id: int, dados: UsuarioDados, db: Session = Depends(get_db)):
    usuario = db.get(Usuario, usuario_id)
    if usuario is None:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado")
    _aplicar_usuario(db, usuario, dados)
    db.commit()
    return _serializar_usuario(usuario)


@router.get("/cep/{cep}", dependencies=[Depends(exigir_admin)])
def consultar_cep(cep: str, numero: str | None = None):
    """Endereco e coordenadas aproximadas do CEP (para o mapa)."""
    return geo.buscar_cep(cep, (numero or "").strip() or None)


@router.delete("/carregadores/{carregador_id}", dependencies=[Depends(exigir_admin)])
def excluir_carregador(carregador_id: str, db: Session = Depends(get_db)):
    """Sem vendas nem recargas: apaga de vez. Com historico: some das telas e
    para de aceitar conexao, mas as vendas antigas ficam nos relatorios."""
    carregador = db.get(Carregador, carregador_id)
    if carregador is None or carregador.excluido_em is not None:
        raise HTTPException(status_code=404, detail="Carregador nao encontrado")
    if sessoes.sessao_ativa(db, carregador_id) is not None:
        raise HTTPException(status_code=409, detail="Tem uma recarga em andamento: encerre antes de excluir")
    tem_historico = (
        db.query(Cobranca.id).filter(Cobranca.carregador_id == carregador_id).first()
        or db.query(SessaoCarga.id).filter(SessaoCarga.carregador_id == carregador_id).first()
    )
    if tem_historico:
        carregador.excluido_em = agora()
        carregador.ativo = False
        registrar_historico(db, carregador_id, "DISPOSITIVO", "Carregador excluido pelo painel (historico mantido)")
        modo = "arquivado"
    else:
        db.query(PacoteTempo).filter(PacoteTempo.carregador_id == carregador_id).delete(synchronize_session=False)
        db.query(HistoricoOperacao).filter(HistoricoOperacao.carregador_id == carregador_id).delete(synchronize_session=False)
        db.delete(carregador)
        modo = "apagado"
    db.commit()
    ocpp_server.desconectar(carregador_id)
    return {"ok": True, "modo": modo}


@router.get("/carregadores/{carregador_id}/mercado-pago/caixa", dependencies=[Depends(exigir_admin)])
def ver_caixa_mp(carregador_id: str, db: Session = Depends(get_db)):
    """Nome atual da loja e do caixa no Mercado Pago."""
    return mercado_pago_caixa.nomes_no_mp(_obter(db, carregador_id))


@router.post("/carregadores/{carregador_id}/mercado-pago/renomear", dependencies=[Depends(exigir_admin)])
def renomear_caixa_mp(carregador_id: str, db: Session = Depends(get_db)):
    """Poe o nome do carregador na loja e no caixa do Mercado Pago."""
    return mercado_pago_caixa.renomear_no_mp(_obter(db, carregador_id))


@router.post("/carregadores/{carregador_id}/travar", dependencies=[Depends(exigir_admin)])
def travar_conector(carregador_id: str, db: Session = Depends(get_db)):
    """Teste manual da trava: deixa o conector Inoperative (nao carrega ao plugar)."""
    carregador = _obter(db, carregador_id)
    if not ocpp_server.esta_conectado(carregador_id):
        raise HTTPException(status_code=409, detail="Carregador offline")
    if sessoes.sessao_ativa(db, carregador_id) is not None:
        raise HTTPException(status_code=409, detail="Tem uma recarga em andamento")
    futuro = ocpp_server.enviar_chamada(carregador_id, "ChangeAvailability", {"connectorId": carregador.connector_id, "type": "Inoperative"})
    try:
        resposta = futuro.result(timeout=OCPP_RESPOSTA_TIMEOUT_SECONDS) if futuro else {}
    except concurrent.futures.TimeoutError:
        raise HTTPException(status_code=504, detail="Carregador nao respondeu")
    except ocpp_server.ErroOcpp as exc:
        raise HTTPException(status_code=502, detail=f"Carregador respondeu erro: {exc.codigo} {exc.descricao}")
    if (resposta or {}).get("status") in trava.RESPOSTAS_OK:
        trava.marcar(carregador_id, True)
    return {"resposta": resposta}


@router.post("/carregadores/{carregador_id}/destravar", dependencies=[Depends(exigir_admin)])
def destravar_conector(carregador_id: str, db: Session = Depends(get_db)):
    """Teste manual: volta o conector a Operative. Sem recarga paga, o worker
    trava de novo em ~30 s (BLOQUEAR_CARGA_SEM_PAGAMENTO)."""
    carregador = _obter(db, carregador_id)
    if not ocpp_server.esta_conectado(carregador_id):
        raise HTTPException(status_code=409, detail="Carregador offline")
    futuro = ocpp_server.enviar_chamada(carregador_id, "ChangeAvailability", {"connectorId": carregador.connector_id, "type": "Operative"})
    try:
        resposta = futuro.result(timeout=OCPP_RESPOSTA_TIMEOUT_SECONDS) if futuro else {}
    except concurrent.futures.TimeoutError:
        raise HTTPException(status_code=504, detail="Carregador nao respondeu")
    except ocpp_server.ErroOcpp as exc:
        raise HTTPException(status_code=502, detail=f"Carregador respondeu erro: {exc.codigo} {exc.descricao}")
    trava.marcar(carregador_id, False)
    return {"resposta": resposta}


@router.post("/sessoes/{sessao_id}/estornar", dependencies=[Depends(exigir_admin)])
def estornar_sessao(sessao_id: int, db: Session = Depends(get_db)):
    """Devolve o pagamento de uma recarga ja encerrada (ex.: o carro nao carregou)."""
    sessao = db.get(SessaoCarga, sessao_id)
    if sessao is None:
        raise HTTPException(status_code=404, detail="Recarga nao encontrada")
    if sessao.status in STATUS_SESSAO_ATIVA:
        raise HTTPException(status_code=409, detail="Encerre a recarga antes de estornar")
    cobranca = db.get(Cobranca, sessao.cobranca_id)
    if cobranca is None or cobranca.valor <= 0:
        raise HTTPException(status_code=422, detail="Recarga sem pagamento para estornar")
    if cobranca.estornada_em is not None:
        raise HTTPException(status_code=409, detail="Ja foi estornada")
    if not pagamentos.estornar_cobranca(db, cobranca):
        db.refresh(cobranca)
        raise HTTPException(status_code=502, detail=cobranca.estorno_erro or "O banco recusou o estorno")
    return {"ok": True}



class CorrenteDados(BaseModel):
    amperes: int = Field(ge=0, le=32)


@router.post("/carregadores/{carregador_id}/corrente", dependencies=[Depends(exigir_admin)])
def definir_corrente(carregador_id: str, dados: CorrenteDados, db: Session = Depends(get_db)):
    """Corrente maxima do carregador (0 a 32 A) por SetChargingProfile.
    Abaixo de 6 A o carro nao carrega (minimo da norma IEC 61851)."""
    carregador = _obter(db, carregador_id)
    if not ocpp_server.esta_conectado(carregador_id):
        raise HTTPException(status_code=409, detail="Carregador offline")
    futuro = trava.aplicar_corrente(carregador_id, dados.amperes)
    try:
        resposta = futuro.result(timeout=OCPP_RESPOSTA_TIMEOUT_SECONDS) if futuro else {}
    except concurrent.futures.TimeoutError:
        raise HTTPException(status_code=504, detail="Carregador nao respondeu")
    except ocpp_server.ErroOcpp as exc:
        raise HTTPException(status_code=502, detail=f"Carregador respondeu erro: {exc.codigo} {exc.descricao}")
    if (resposta or {}).get("status") == "Accepted":
        carregador.corrente_maxima_a = dados.amperes
        registrar_historico(db, carregador_id, "DISPOSITIVO", f"Corrente maxima definida em {dados.amperes} A")
        db.commit()
    return {"resposta": resposta, "corrente_maxima_a": carregador.corrente_maxima_a}
