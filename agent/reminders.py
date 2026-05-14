# agent/reminders.py — Cron job de recordatorios de vencimiento

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from agent.memory import Cliente, HistorialRecordatorio, PlantillaRecordatorio, async_session

logger = logging.getLogger("agentkit")

_TZ_CDMX = ZoneInfo("America/Mexico_City")

# Instancia global del scheduler — se arranca/para en lifespan de main.py
scheduler = AsyncIOScheduler(timezone="America/Mexico_City")

# Proveedor lazy para no crear dependencia circular con main.py
_proveedor = None


def _get_proveedor():
    global _proveedor
    if _proveedor is None:
        from agent.providers import obtener_proveedor
        _proveedor = obtener_proveedor()
    return _proveedor


async def enviar_recordatorios_diarios() -> None:
    """
    Cron job: ejecuta diariamente a las 10:00 AM hora México.
    Para cada cliente activo sin pausa, envía el recordatorio si corresponde
    a 7 días, 1 día o el día de vencimiento. Deduplica por día CDMX.
    """
    hoy_cdmx = datetime.now(_TZ_CDMX).date()

    # Límites del día en UTC para deduplicación (CDMX = UTC-6)
    inicio_utc = datetime(
        hoy_cdmx.year, hoy_cdmx.month, hoy_cdmx.day, 0, 0, 0, tzinfo=_TZ_CDMX
    ).astimezone(timezone.utc).replace(tzinfo=None)
    fin_utc = datetime(
        hoy_cdmx.year, hoy_cdmx.month, hoy_cdmx.day, 23, 59, 59, tzinfo=_TZ_CDMX
    ).astimezone(timezone.utc).replace(tzinfo=None)

    _TIPOS = {7: "7_dias", 1: "1_dia", 0: "vencimiento"}

    async with async_session() as session:
        result = await session.execute(
            select(Cliente).where(
                Cliente.activo == True,
                Cliente.recordatorios_pausados == False,
            )
        )
        clientes = result.scalars().all()

    enviados = omitidos = errores = 0

    for cliente in clientes:
        dias = (cliente.fecha_vencimiento - hoy_cdmx).days
        tipo = _TIPOS.get(dias)
        if not tipo:
            continue

        # Deduplicar: ¿ya se envió hoy este tipo para este cliente?
        async with async_session() as session:
            dup = await session.execute(
                select(HistorialRecordatorio).where(
                    HistorialRecordatorio.cliente_id == cliente.id,
                    HistorialRecordatorio.tipo == tipo,
                    HistorialRecordatorio.enviado_en >= inicio_utc,
                    HistorialRecordatorio.enviado_en <= fin_utc,
                )
            )
            if dup.scalars().first():
                omitidos += 1
                continue

        # Obtener plantilla
        async with async_session() as session:
            plantilla = await session.get(PlantillaRecordatorio, (tipo, cliente.producto))
        if not plantilla:
            logger.warning(f"Sin plantilla {tipo}/{cliente.producto} — omitiendo {cliente.nombre}")
            omitidos += 1
            continue

        # Formatear mensaje
        try:
            mensaje = plantilla.mensaje.format(
                nombre=cliente.nombre,
                producto=cliente.producto,
                usuario_app=cliente.usuario_app or cliente.nombre,
                fecha_vencimiento=cliente.fecha_vencimiento.strftime("%d/%m/%Y"),
                dias_restantes=max(dias, 0),
            )
        except (KeyError, ValueError) as e:
            logger.error(f"Error formateando plantilla {tipo}/{cliente.producto}: {e}")
            errores += 1
            continue

        # Enviar
        proveedor = _get_proveedor()
        ok = await proveedor.enviar_mensaje(cliente.telefono, mensaje)

        if ok:
            async with async_session() as session:
                session.add(HistorialRecordatorio(
                    cliente_id=cliente.id,
                    tipo=tipo,
                    enviado_en=datetime.utcnow(),
                    mensaje_enviado=mensaje,
                ))
                await session.commit()
            logger.info(f"Recordatorio {tipo} → {cliente.nombre} ({cliente.telefono})")
            enviados += 1
        else:
            logger.error(f"Fallo envío recordatorio a {cliente.telefono}")
            errores += 1

    logger.info(
        f"Recordatorios completados — enviados: {enviados}, omitidos: {omitidos}, errores: {errores}"
    )
