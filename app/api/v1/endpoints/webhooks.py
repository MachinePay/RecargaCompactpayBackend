import logging

from fastapi import APIRouter, BackgroundTasks, Request

from app.services import pagamentos

router = APIRouter(tags=["webhooks"])


@router.post("/callback-mercado-pago")
async def callback_mercado_pago(request: Request, background: BackgroundTasks):
    """Notificacao do MP (order do tablet ou payment da maquininha do caixa).
    Responde 200 na hora e reconsulta em segundo plano (o conteudo do webhook
    nao e' usado como verdade)."""
    try:
        dados = await request.json()
    except Exception:
        dados = {}
    if not dados.get("data"):
        # Formatos antigos na query string: ?type=order&data.id=... ou ?topic=payment&id=...
        query = request.query_params
        dados = {
            **dados,
            "type": query.get("type") or query.get("topic") or dados.get("type") or dados.get("topic"),
            "data": {"id": query.get("data.id") or query.get("id") or dados.get("id")},
        }
    logging.info("[MP webhook] %s", dados)
    background.add_task(pagamentos.processar_webhook, dados)
    return {"ok": True}
