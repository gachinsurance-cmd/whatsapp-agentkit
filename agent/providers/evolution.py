import os
import logging
import httpx
from fastapi import Request
from agent.providers.base import ProveedorWhatsApp, MensajeEntrante

logger = logging.getLogger("agentkit")


class ProveedorEvolution(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Evolution API v2."""

    def __init__(self):
        self.api_url = os.getenv("EVOLUTION_API_URL", "").rstrip("/")
        self.api_key = os.getenv("EVOLUTION_API_KEY", "")
        self.instance = os.getenv("EVOLUTION_INSTANCE", "activabot")

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Parsea eventos messages.upsert de Evolution API v2."""
        body = await request.json()
        mensajes = []

        event = body.get("event", "")
        if event != "messages.upsert":
            return mensajes

        data = body.get("data", {})
        key = data.get("key", {})
        message = data.get("message", {})

        texto = (
            message.get("conversation")
            or message.get("extendedTextMessage", {}).get("text")
            or ""
        )
        telefono = key.get("remoteJid", "").replace("@s.whatsapp.net", "").replace("@g.us", "")
        mensaje_id = key.get("id", "")
        es_propio = key.get("fromMe", False)

        if texto and telefono:
            mensajes.append(MensajeEntrante(
                telefono=telefono,
                texto=texto,
                mensaje_id=mensaje_id,
                es_propio=es_propio,
            ))

        return mensajes

    async def enviar_mensaje(self, telefono: str, mensaje: str) -> bool:
        """Envía mensaje via Evolution API v2."""
        if not self.api_url or not self.api_key:
            logger.warning("EVOLUTION_API_URL o EVOLUTION_API_KEY no configurados")
            return False

        url = f"{self.api_url}/message/sendText/{self.instance}"
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "number": telefono,
            "text": mensaje,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code not in (200, 201):
                logger.error(f"Error Evolution API: {r.status_code} — {r.text}")
            return r.status_code in (200, 201)
