# agent/main.py — Servidor FastAPI + Webhook de WhatsApp

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from agent.admin import router as admin_router
from agent.admin_commands import es_comando_admin, ejecutar_comando
from agent.brain import generar_respuesta, inicializar_knowledge
from agent.escalation import (
    chequear_auto_desescalacion,
    desescalar,
    escalar,
    esta_bloqueado,
    esta_escalado,
    mensaje_escalacion,
)
from agent.fallas import cargar_fallas_desde_db, obtener_fallas_activas
from agent.memory import guardar_mensaje, inicializar_db, obtener_historial
from agent.providers import obtener_proveedor
from agent.startup import migrar_knowledge
from agent.tools import procesar_instalacion

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))

# Palabras que indican el cliente quiere hablar con un humano → escalación inmediata
_KEYWORDS_HUMANO = {
    # Pedir atención humana
    "humano", "asesor", "persona", "administrador",
    # Intención de pago / renovación
    "renovar", "renovacion", "renovación",
    "activar", "activacion", "activación", "actívame",
    "reactivar", "reactivacion", "reactivación",
    "comprobante", "pago", "pagué", "pague", "pagar",
    "deposito", "depósito", "transferencia", "transferi", "transferí",
    "listo",
}

# Palabras que reportan un problema → escalar SOLO si no hay falla conocida del servicio
_KEYWORDS_PROBLEMA = {"falla", "no funciona", "no jala", "no sirve"}

# Mensaje para clientes que envían media sin texto
_MSG_SIN_TEXTO = (
    "¡Hola! Recibí tu mensaje. Para poder ayudarte mejor, "
    "¿me puedes escribir lo que necesitas? 😊"
)


def _pide_humano(texto: str) -> bool:
    t = texto.lower()
    return any(kw in t for kw in _KEYWORDS_HUMANO)


def _reporta_problema(texto: str) -> bool:
    t = texto.lower()
    return any(kw in t for kw in _KEYWORDS_PROBLEMA)


def _menciona_servicio_con_falla(texto: str) -> bool:
    """True si el texto menciona un servicio que tiene falla activa en este momento."""
    t = texto.lower()
    return any(s.lower() in t for s in obtener_fallas_activas())


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    migrar_knowledge()
    inicializar_knowledge()
    await cargar_fallas_desde_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — ActivaBot (Activaciones Garza)",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(admin_router)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "activabot"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


async def _procesar_mensajes(request: Request):
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            telefono = msg.telefono

            # ── 1. fromMe: verificar desescalación, luego ignorar siempre ──────
            if msg.es_propio:
                if await esta_escalado(telefono):
                    await desescalar(telefono)
                    logger.info(f"Desescalado por mensaje propio (admin escribió a cliente): {telefono}")
                continue

            # ── 2. Número bloqueado ──────────────────────────────────────────
            if await esta_bloqueado(telefono):
                logger.info(f"Ignorado (bloqueado): {telefono}")
                if ADMIN_PHONE:
                    aviso = (
                        f"📵 Número bloqueado escribió:\n"
                        f"Cliente: {telefono}\n"
                        f"Mensaje: {msg.texto or '[sin texto]'}"
                    )
                    await proveedor.enviar_mensaje(ADMIN_PHONE, aviso)
                continue

            # ── 3. Comando admin (#falla, #resuelto, #estado) ────────────────
            if msg.texto and es_comando_admin(telefono, msg.texto):
                respuesta_cmd = await ejecutar_comando(msg.texto, proveedor)
                await proveedor.enviar_mensaje(telefono, respuesta_cmd)
                logger.info(f"Comando admin ejecutado por {telefono}: {msg.texto[:50]}")
                continue

            # ── 4. Cliente escalado: verificar auto-desescalación ────────────
            if await esta_escalado(telefono):
                puede_continuar = await chequear_auto_desescalacion(telefono)
                if not puede_continuar:
                    logger.info(f"Mensaje bloqueado — cliente escalado: {telefono}")
                    continue
                # puede_continuar=True → fue auto-desescalado, continúa flujo normal

            # ── 5. Sin texto (imagen, audio, video, sticker) ─────────────────
            if not msg.texto:
                await proveedor.enviar_mensaje(telefono, _MSG_SIN_TEXTO)
                logger.info(f"Saludo por {msg.tipo} sin texto: {telefono}")
                continue

            logger.info(f"Mensaje de {telefono}: {msg.texto[:80]}")

            # ── 6a. Keywords de humano → escalación inmediata ─────────────────
            if _pide_humano(msg.texto):
                await escalar(telefono, "keyword", msg.texto, proveedor)
                resp_esc = mensaje_escalacion()
                await proveedor.enviar_mensaje(telefono, resp_esc)
                await guardar_mensaje(telefono, "user", msg.texto)
                await guardar_mensaje(telefono, "assistant", resp_esc)
                continue

            # ── 6b. Keywords de problema → escalar solo si no hay falla activa
            if _reporta_problema(msg.texto) and not _menciona_servicio_con_falla(msg.texto):
                await escalar(telefono, "keyword", msg.texto, proveedor)
                resp_esc = mensaje_escalacion()
                await proveedor.enviar_mensaje(telefono, resp_esc)
                await guardar_mensaje(telefono, "user", msg.texto)
                await guardar_mensaje(telefono, "assistant", resp_esc)
                continue

            # ── 7. Generar respuesta con Claude ───────────────────────────────
            historial = await obtener_historial(telefono)
            respuesta = await generar_respuesta(msg.texto, historial)
            respuesta = await procesar_instalacion(respuesta, telefono)

            # ── 8. Detectar señal [ESCALAR] de Claude ─────────────────────────
            if "[ESCALAR]" in respuesta:
                respuesta = respuesta.replace("[ESCALAR]", "").strip()
                await escalar(telefono, "bot_no_sabe", msg.texto, proveedor)
                respuesta = mensaje_escalacion()
                logger.info(f"Escalado por [ESCALAR] de Claude: {telefono}")

            # ── 9. Enviar respuesta ───────────────────────────────────────────
            await guardar_mensaje(telefono, "user", msg.texto)
            await guardar_mensaje(telefono, "assistant", respuesta)
            await proveedor.enviar_mensaje(telefono, respuesta)
            logger.info(f"Respuesta enviada a {telefono}: {respuesta[:60]}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/webhook")
async def webhook_handler(request: Request):
    return await _procesar_mensajes(request)


@app.get("/webhook/messages")
@app.get("/webhook/messages/messages")
async def webhook_messages_verificacion(request: Request):
    return {"status": "ok"}


@app.post("/webhook/messages")
async def webhook_messages_handler(request: Request):
    return await _procesar_mensajes(request)


@app.post("/webhook/messages/messages")
async def webhook_messages_messages_handler(request: Request):
    return await _procesar_mensajes(request)
