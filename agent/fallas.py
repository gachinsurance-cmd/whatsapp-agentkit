# agent/fallas.py — Estado de fallas de servicios

import logging
from datetime import datetime
from sqlalchemy import select

from agent.memory import async_session, FallaServicio

logger = logging.getLogger("agentkit")

# Servicios soportados. Agregar aquí para extender.
SERVICIOS_CONOCIDOS: set[str] = {"AztkPlay", "LamTV"}

# Caché en memoria: {servicio: razon} — solo fallas activas
_fallas: dict[str, str] = {}


async def cargar_fallas_desde_db() -> None:
    """Precarga fallas activas desde DB al arrancar. Llamar en lifespan."""
    async with async_session() as session:
        result = await session.execute(
            select(FallaServicio).where(FallaServicio.activa == True)
        )
        _fallas.clear()
        for f in result.scalars().all():
            _fallas[f.servicio] = f.razon
    if _fallas:
        logger.info(f"Fallas activas cargadas: {list(_fallas.keys())}")
    else:
        logger.info("Sin fallas activas al arrancar")


async def registrar_falla(servicio: str, razon: str) -> None:
    """Registra o actualiza una falla activa para un servicio."""
    async with async_session() as session:
        existente = await session.get(FallaServicio, servicio)
        if existente:
            existente.razon = razon
            existente.activa = True
            existente.creado_en = datetime.utcnow()
            existente.resuelto_en = None
        else:
            session.add(FallaServicio(servicio=servicio, razon=razon))
        await session.commit()
    _fallas[servicio] = razon
    logger.info(f"Falla registrada: {servicio} — {razon or 'sin descripción'}")


async def resolver_falla(servicio: str) -> bool:
    """Marca una falla como resuelta. Retorna True si existía y estaba activa."""
    async with async_session() as session:
        existente = await session.get(FallaServicio, servicio)
        if not existente or not existente.activa:
            return False
        existente.activa = False
        existente.resuelto_en = datetime.utcnow()
        await session.commit()
    _fallas.pop(servicio, None)
    logger.info(f"Falla resuelta: {servicio}")
    return True


def obtener_fallas_activas() -> dict[str, str]:
    """Retorna fallas activas {servicio: razon}. Sync — usa caché en memoria."""
    return dict(_fallas)


def get_fallas_texto() -> str:
    """Texto para inyectar en system prompt. Sync."""
    if not _fallas:
        return ""
    lineas = [f"- {s}: {r}" if r else f"- {s}" for s, r in _fallas.items()]
    return "\n".join(lineas)


async def listar_todas_fallas() -> list[dict]:
    """Lista completa (activas e inactivas) para el panel admin."""
    async with async_session() as session:
        result = await session.execute(
            select(FallaServicio).order_by(FallaServicio.servicio)
        )
        return [
            {
                "servicio": f.servicio,
                "razon": f.razon,
                "activa": f.activa,
                "creado_en": f.creado_en.isoformat() + "Z",
                "resuelto_en": f.resuelto_en.isoformat() + "Z" if f.resuelto_en else None,
            }
            for f in result.scalars().all()
        ]
