# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

import os
import re
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "knowledge")

# Caché en memoria: {nombre_archivo: {"mtime": float, "content": str}}
_knowledge_cache: dict[str, dict] = {}


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def _leer_archivo(archivo: str, ruta: str) -> str:
    """Extrae texto de un archivo según su extensión."""
    ext = archivo.lower().rsplit(".", 1)[-1]
    try:
        if ext == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(ruta)
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        elif ext in ("txt", "md", "csv", "json", "yaml", "yml"):
            with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        logger.warning(f"No se pudo leer {archivo}: {e}")
    return ""


def inicializar_knowledge():
    """Precarga todos los archivos de KNOWLEDGE_DIR al arrancar el servidor."""
    if not os.path.exists(KNOWLEDGE_DIR):
        return

    for archivo in sorted(os.listdir(KNOWLEDGE_DIR)):
        if archivo.startswith("."):
            continue
        ruta = os.path.join(KNOWLEDGE_DIR, archivo)
        if not os.path.isfile(ruta):
            continue

        mtime = os.path.getmtime(ruta)
        texto = _leer_archivo(archivo, ruta)
        if texto.strip():
            _knowledge_cache[archivo] = {"mtime": mtime, "content": texto.strip()}
            logger.info(f"Knowledge cargado al inicio: {archivo} ({len(texto)} chars)")

    logger.info(f"Knowledge listo: {len(_knowledge_cache)} archivo(s) en caché")


def leer_knowledge() -> str:
    """
    Retorna el contenido de KNOWLEDGE_DIR desde caché.
    Verifica mtime en cada llamada y recarga solo los archivos que cambiaron.
    """
    if not os.path.exists(KNOWLEDGE_DIR):
        return ""

    archivos_en_disco: set[str] = set()

    for archivo in sorted(os.listdir(KNOWLEDGE_DIR)):
        if archivo.startswith("."):
            continue
        ruta = os.path.join(KNOWLEDGE_DIR, archivo)
        if not os.path.isfile(ruta):
            continue

        archivos_en_disco.add(archivo)
        mtime = os.path.getmtime(ruta)
        cached = _knowledge_cache.get(archivo)

        if cached and cached["mtime"] == mtime:
            continue  # Sin cambios — usar caché

        texto = _leer_archivo(archivo, ruta)
        if texto.strip():
            accion = "recargado" if archivo in _knowledge_cache else "cargado"
            _knowledge_cache[archivo] = {"mtime": mtime, "content": texto.strip()}
            logger.info(f"Knowledge {accion}: {archivo} ({len(texto)} chars)")

    # Eliminar del caché archivos que ya no existen en disco
    eliminados = set(_knowledge_cache.keys()) - archivos_en_disco
    for archivo in eliminados:
        del _knowledge_cache[archivo]
        logger.info(f"Knowledge eliminado del caché: {archivo}")

    contenidos = [
        f"### {nombre}\n{data['content']}"
        for nombre, data in sorted(_knowledge_cache.items())
    ]
    return "\n\n".join(contenidos)


def cargar_system_prompt() -> str:
    """Lee el system prompt y agrega knowledge, fallas activas e instrucción de escalación."""
    config = cargar_config_prompts()
    base = config.get("system_prompt", "Eres un asistente útil. Responde en español.")

    conocimiento = leer_knowledge()
    if conocimiento:
        base += f"\n\n## Información del negocio (documentos)\n{conocimiento}"

    # Inyectar fallas activas (sync — caché en memoria de agent/fallas.py)
    from agent.fallas import get_fallas_texto
    fallas = get_fallas_texto()
    if fallas:
        base += (
            f"\n\n## Servicios con falla activa en este momento\n{fallas}\n"
            "Si el cliente pregunta por cualquiera de estos servicios, responde exactamente: "
            "\"Ya está reportado al proveedor. Te aviso cuando se restablezca 🙏\""
        )

    base += (
        "\n\n## Cuándo pedir atención humana\n"
        "Si el cliente hace una pregunta que genuinamente no puedes responder con tu información, "
        "o necesita atención personalizada más allá de tu conocimiento, "
        "agrega exactamente \"[ESCALAR]\" al FINAL de tu respuesta — sin mencionárselo al cliente."
    )

    return base


def limpiar_formato(texto: str) -> str:
    """
    Convierte markdown estándar a formato WhatsApp y limpia URLs.
    1. Elimina asteriscos que rodean URLs (links deben ir sin formato)
    2. Convierte **negritas** a *negritas* (un solo asterisco — WhatsApp)
    """
    texto = re.sub(r'\*+(https?://[^\s*]+)\*+', r'\1', texto)
    texto = re.sub(r'\*\*(.+?)\*\*', r'*\1*', texto, flags=re.DOTALL)
    return texto


def obtener_mensaje_error() -> str:
    config = cargar_config_prompts()
    return config.get("error_message", "Uy, tuve un problemita técnico. Intenta de nuevo en un momento, por favor.")


def obtener_mensaje_fallback() -> str:
    config = cargar_config_prompts()
    return config.get("fallback_message", "Hmm, no entendí bien eso. ¿Me lo puedes explicar de otra forma?")


async def generar_respuesta(mensaje: str, historial: list[dict]) -> str:
    """
    Genera una respuesta usando Claude API.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]

    Returns:
        La respuesta generada por Claude
    """
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    system_prompt = cargar_system_prompt()

    mensajes = []
    for msg in historial:
        mensajes.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    mensajes.append({
        "role": "user",
        "content": mensaje
    })

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            messages=mensajes
        )

        respuesta = response.content[0].text
        respuesta = limpiar_formato(respuesta)
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
