import logging

from fastapi import APIRouter, BackgroundTasks, Request

from app.services import pagamentos

router = APIRouter(tags=["webhooks"])


@router.post("/callback-mercado-pago")
async def callback_mercado_pago(request: Request, background: BackgroundTasks):
    """Notificacao de order do MP. Responde 200 na hora e reconsulta a order
    em segundo plano (o conteudo do webhook nao e' usado como verdade)."""
    try:
        dados = await request.json()
    except Exception:
        dados = {}
    if not dados.get("data"):
        # Formato antigo de notificacao: ?data.id=...&type=order na query string.
        dados = {"type": request.query_params.get("type"), "data": {"id": request.query_params.get("data.id")}}
    logging.info("[MP webhook] %s", dados)
    background.add_task(pagamentos.processar_webhook, dados)
    return {"ok": True}
