"""Login do painel com e-mail e senha."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin import Acesso, acesso_atual, get_db
from app.models.carregador import Usuario, agora
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginDados(BaseModel):
    email: str
    senha: str


@router.post("/login")
def login(dados: LoginDados, db: Session = Depends(get_db)):
    usuario = db.query(Usuario).filter(Usuario.email == dados.email.strip().lower()).first()
    # Mesma resposta para e-mail ou senha errados: nao entrega quais e-mails existem.
    if usuario is None or not usuario.ativo or not auth.conferir_senha(dados.senha, usuario.senha_hash):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos")
    usuario.ultimo_login = agora()
    db.commit()
    return {
        "token": auth.criar_token(usuario),
        "usuario": {"id": usuario.id, "nome": usuario.nome, "email": usuario.email, "role": usuario.role, "cliente_id": usuario.cliente_id},
    }


@router.get("/me")
def eu(acesso: Acesso = Depends(acesso_atual)):
    return {"role": acesso.role, "cliente_id": acesso.cliente_id, "usuario_id": acesso.usuario_id, "nome": acesso.nome}
