# agent/tools.py — Herramientas del agente para Activaciones Garza
# Generado por AgentKit

import os
import re as _re
import yaml
import logging
import httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención del negocio."""
    info = cargar_info_negocio()
    ahora = datetime.now()
    hora_actual = ahora.hour + ahora.minute / 60
    esta_abierto = 9.0 <= hora_actual <= 22.5  # 9am a 10:30pm
    return {
        "horario": info.get("negocio", {}).get("horario", "Lunes a Domingo 9am a 10:30pm"),
        "esta_abierto": esta_abierto,
    }


def buscar_en_knowledge(consulta: str) -> str:
    """Busca información relevante en los archivos de /knowledge."""
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ── Toma de pedidos ──────────────────────────────────────────
_pedidos_en_curso: dict[str, dict] = {}


def iniciar_pedido(telefono: str, app: str, duracion: str) -> dict:
    """Registra un nuevo pedido de activación."""
    precios = {
        "lamtv": {"1": 200, "3": 500, "6": 900, "12": 1650},
        "aztkplay": {"1": 200, "3": 550, "6": 1100, "12": 1800},
    }
    app_key = app.lower().replace(" ", "")
    precio = precios.get(app_key, {}).get(str(duracion), None)

    pedido = {
        "telefono": telefono,
        "app": app,
        "duracion_meses": duracion,
        "precio": precio,
        "estado": "pendiente_pago",
        "creado": datetime.now().isoformat(),
    }
    _pedidos_en_curso[telefono] = pedido
    return pedido


def obtener_pedido(telefono: str) -> dict | None:
    """Recupera el pedido en curso de un cliente."""
    return _pedidos_en_curso.get(telefono)


def confirmar_pago(telefono: str) -> bool:
    """Marca el pedido como pago recibido, pendiente de activación."""
    if telefono in _pedidos_en_curso:
        _pedidos_en_curso[telefono]["estado"] = "pago_recibido"
        return True
    return False


# ── Solicitudes de instalación personal ──────────────────────
_instalaciones_pendientes: list[dict] = []


async def _enviar_alerta_ntfy(nombre: str, telefono_cliente: str, dispositivo: str, app: str):
    """Envía notificación push via ntfy.sh."""
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        logger.warning("NTFY_TOPIC no configurado — alerta no enviada")
        return

    hora = datetime.now().strftime("%H:%M")
    mensaje = (
        f"👤 {nombre} | 📱 {telefono_cliente}\n"
        f"📺 {dispositivo} | 📦 {app}\n"
        f"🕐 {hora}"
    )

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://ntfy.sh/{topic}",
                content=mensaje.encode("utf-8"),
                headers={
                    "Title": "Solicitud de instalacion - ActivaBot",
                    "Priority": "urgent",
                    "Tags": "bell",
                },
                timeout=10,
            )
            if r.status_code == 200:
                logger.info("Alerta de instalación enviada via ntfy")
            else:
                logger.error(f"Error ntfy: {r.status_code} — {r.text}")
    except Exception as e:
        logger.error(f"Error enviando alerta ntfy: {e}")


async def registrar_solicitud_instalacion(telefono: str, nombre: str, dispositivo: str, app: str):
    """
    Registra una solicitud de instalación y notifica al dueño por WhatsApp.
    """
    solicitud = {
        "telefono": telefono,
        "nombre": nombre,
        "dispositivo": dispositivo,
        "app": app,
        "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "atendida": False,
    }
    _instalaciones_pendientes.append(solicitud)

    separador = "=" * 55
    logger.warning(separador)
    logger.warning("  NUEVA SOLICITUD DE INSTALACION PERSONAL")
    logger.warning(f"  Telefono : {telefono}")
    logger.warning(f"  Nombre   : {nombre}")
    logger.warning(f"  App      : {app}")
    logger.warning(f"  Dispositivo: {dispositivo}")
    logger.warning(f"  Hora     : {solicitud['hora']}")
    logger.warning(separador)

    await _enviar_alerta_ntfy(nombre, telefono, dispositivo, app)


def obtener_instalaciones_pendientes() -> list[dict]:
    """Retorna la lista de solicitudes de instalación no atendidas."""
    return [s for s in _instalaciones_pendientes if not s["atendida"]]


# ── Procesador de etiquetas en respuestas del agente ─────────
_PATRON_INSTALACION = _re.compile(
    r"\[INSTALACION_SOLICITADA:nombre=([^,\]]+),dispositivo=([^,\]]+),app=([^\]]+)\]",
    _re.IGNORECASE,
)


async def procesar_instalacion(respuesta: str, telefono: str) -> str:
    """
    Busca [INSTALACION_SOLICITADA:...] en la respuesta, envía alerta WhatsApp y elimina la etiqueta.
    Retorna la respuesta limpia para enviar al cliente.
    """
    match = _PATRON_INSTALACION.search(respuesta)
    if match:
        nombre = match.group(1).strip()
        dispositivo = match.group(2).strip()
        app = match.group(3).strip()
        await registrar_solicitud_instalacion(telefono, nombre, dispositivo, app)
        respuesta = _PATRON_INSTALACION.sub("", respuesta).strip()
    return respuesta
