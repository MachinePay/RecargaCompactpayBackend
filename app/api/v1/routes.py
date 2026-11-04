from fastapi import APIRouter

from app.api.v1.endpoints import admin, tablet, webhooks, whatsapp

api_router = APIRouter()
api_router.include_router(tablet.router)
api_router.include_router(admin.router)
api_router.include_router(whatsapp.router)
api_router.include_router(webhooks.router)
