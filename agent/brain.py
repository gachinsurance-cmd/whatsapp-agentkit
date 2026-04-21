# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

import os
import yaml
import logging
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def leer_knowledge() -> str:
    """
    Lee todos los archivos de /knowledge automáticamente.
    Soporta: PDF, TXT, MD, CSV, JSON.
    Sin necesidad de actualizar el código al agregar archivos.
    """
    knowledge_dir = "knowledge"
    if not os.path.exists(knowledge_dir):
        return ""

    contenidos = []

    for archivo in sorted(os.listdir(knowledge_dir)):
        if archivo.startswith("."):
            continue
        ruta = os.path.join(knowledge_dir, archivo)
        if not os.path.isfile(ruta):
            continue

        ext = archivo.lower().split(".")[-1]
        texto = ""

        try:
            if ext == "pdf":
                from pypdf import PdfReader
                reader = PdfReader(ruta)
                texto = "\n".join(page.extract_text() or "" for page in reader.pages)
            elif ext in ("txt", "md", "csv", "json", "yaml", "yml"):
                with open(ruta, "r", encoding="utf-8", errors="ignore") as f:
                    texto = f.read()
        except Exception as e:
            logger.warning(f"No se pudo leer {archivo}: {e}")
            continue

        if texto.strip():
            contenidos.append(f"### {archivo}\n{texto.strip()}")
            logger.debug(f"Knowledge cargado: {archivo} ({len(texto)} chars)")

    return "\n\n".join(contenidos)


def cargar_system_prompt() -> str:
    """Lee el system prompt y agrega el contenido de /knowledge automáticamente."""
    config = cargar_config_prompts()
    base = config.get("system_prompt", "Eres un asistente útil. Responde en español.")

    conocimiento = leer_knowledge()
    if conocimiento:
        base += f"\n\n## Información del negocio (documentos)\n{conocimiento}"

    return base


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
        logger.info(f"Respuesta generada ({response.usage.input_tokens} in / {response.usage.output_tokens} out)")
        return respuesta

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
