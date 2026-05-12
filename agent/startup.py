# agent/startup.py — Lógica de arranque del servidor
# Generado por AgentKit

import os
import shutil
import logging

logger = logging.getLogger("agentkit")

# Directorio baked en la imagen Docker (fuente para la migración inicial)
_SOURCE_DIR = "knowledge"


def migrar_knowledge() -> None:
    """
    Migra archivos de knowledge de la imagen Docker al volumen persistente.

    Solo corre si KNOWLEDGE_DIR apunta a un directorio distinto al de la imagen.
    Si el volumen ya tiene archivos, no hace nada (idempotente).
    Si SOURCE_DIR no existe, loggea y continúa sin crashear.
    """
    knowledge_dir = os.getenv("KNOWLEDGE_DIR", _SOURCE_DIR)

    # Sin volumen externo configurado — nada que migrar
    if os.path.abspath(knowledge_dir) == os.path.abspath(_SOURCE_DIR):
        logger.info("KNOWLEDGE_DIR apunta al directorio local — migración omitida")
        return

    # Crear directorio destino si no existe
    try:
        os.makedirs(knowledge_dir, exist_ok=True)
    except Exception as e:
        logger.error(f"No se pudo crear {knowledge_dir}: {e} — se usará '{_SOURCE_DIR}'")
        return

    # Si el volumen ya tiene archivos, ya fue migrado en un deploy anterior
    archivos_en_destino = [
        f for f in os.listdir(knowledge_dir)
        if not f.startswith(".") and os.path.isfile(os.path.join(knowledge_dir, f))
    ]
    if archivos_en_destino:
        logger.info(f"Volumen ya migrado: {len(archivos_en_destino)} archivo(s) en {knowledge_dir}")
        return

    # SOURCE_DIR puede no existir si alguien lo borró del repo
    if not os.path.exists(_SOURCE_DIR):
        logger.warning(f"'{_SOURCE_DIR}' no existe en la imagen — migración omitida, volumen vacío")
        return

    try:
        copiados = 0
        for archivo in sorted(os.listdir(_SOURCE_DIR)):
            if archivo.startswith("."):
                continue
            src = os.path.join(_SOURCE_DIR, archivo)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(knowledge_dir, archivo)
            shutil.copy2(src, dst)  # copy2 preserva mtime — importante para el caché
            copiados += 1
            logger.info(f"Knowledge migrado: {archivo}")

        logger.info(f"Migración completa: {copiados} archivo(s) copiados a {knowledge_dir}")

    except Exception as e:
        logger.error(f"Error durante migración de knowledge: {e} — el bot continuará con '{_SOURCE_DIR}'")
