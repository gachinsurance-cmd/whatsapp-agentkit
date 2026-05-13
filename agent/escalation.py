# agent/escalation.py — Escalación a humano y lista negra

import os
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select

from agent.memory import async_session, Escalacion, NumeroBloqueado

logger = logging.getLogger("agentkit")

ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")

_TZ_CDMX = ZoneInfo("America/Mexico_City")
_HORA_APERTURA = 9.0    # 9:00 AM
_HORA_CIERRE = 22.5     # 10:30 PM
_MINUTOS_AUTO_DESESCALAR = 30


# ── Horario ────────────────────────────────────────────────────────────────────

def esta_en_horario() -> bool:
    """True si estamos dentro del horario de atención (9 AM – 10:30 PM CDMX)."""
    now = datetime.now(_TZ_CDMX)
    hora = now.hour + now.minute / 60
    return _HORA_APERTURA <= hora <= _HORA_CIERRE


def mensaje_escalacion() -> str:
    """Retorna el mensaje de escalación según el horario actual."""
    if esta_en_horario():
        return "Te comunico con el administrador. En breve responde 🙌"
    return "Horario de atención del administrador: 9 AM - 10:30 PM. Te respondo personalmente mañana 🙌"


# ── Escalación ─────────────────────────────────────────────────────────────────

async def esta_escalado(telefono: str) -> bool:
    async with async_session() as session:
        return await session.get(Escalacion, telefono) is not None


async def escalar(telefono: str, razon: str, ultimo_mensaje: str, proveedor) -> None:
    """Escala un cliente: persiste en DB y notifica al admin por WhatsApp."""
    now = datetime.utcnow()
    async with async_session() as session:
        existente = await session.get(Escalacion, telefono)
        if existente:
            existente.razon = razon
            existente.ultimo_mensaje = ultimo_mensaje[:1000]
            existente.escalado_en = now
            existente.ultimo_contacto = now
        else:
            session.add(Escalacion(
                telefono=telefono,
                razon=razon,
                ultimo_mensaje=ultimo_mensaje[:1000],
                escalado_en=now,
                ultimo_contacto=now,
            ))
        await session.commit()

    if ADMIN_PHONE:
        razon_label = {
            "keyword": "palabra clave detectada",
            "bot_no_sabe": "bot no pudo responder",
        }.get(razon, razon)
        notif = (
            f"⚠️ Escalación — {razon_label}\n"
            f"Cliente: {telefono}\n"
            f"Mensaje: {ultimo_mensaje[:200]}"
        )
        await proveedor.enviar_mensaje(ADMIN_PHONE, notif)

    logger.info(f"Escalado: {telefono} ({razon})")


async def desescalar(telefono: str) -> bool:
    """Elimina la escalación activa. Retorna True si existía."""
    async with async_session() as session:
        existente = await session.get(Escalacion, telefono)
        if not existente:
            return False
        await session.delete(existente)
        await session.commit()
    logger.info(f"Desescalado: {telefono}")
    return True


async def chequear_auto_desescalacion(telefono: str) -> bool:
    """
    Verifica si el cliente puede ser procesado normalmente.
    - Si pasaron 30 min desde su último mensaje → desescala y retorna True
    - Si sigue en ventana de escalación → actualiza timestamp y retorna False
    """
    async with async_session() as session:
        esc = await session.get(Escalacion, telefono)
        if not esc:
            return True

        inactivo_por = datetime.utcnow() - esc.ultimo_contacto
        if inactivo_por > timedelta(minutes=_MINUTOS_AUTO_DESESCALAR):
            await session.delete(esc)
            await session.commit()
            logger.info(f"Auto-desescalado por {_MINUTOS_AUTO_DESESCALAR} min inactivo: {telefono}")
            return True

        esc.ultimo_contacto = datetime.utcnow()
        await session.commit()
        return False


async def listar_escalaciones() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(Escalacion).order_by(Escalacion.escalado_en.desc())
        )
        return [
            {
                "telefono": e.telefono,
                "razon": e.razon,
                "ultimo_mensaje": e.ultimo_mensaje,
                "escalado_en": e.escalado_en.isoformat() + "Z",
                "ultimo_contacto": e.ultimo_contacto.isoformat() + "Z",
            }
            for e in result.scalars().all()
        ]


# ── Lista negra ────────────────────────────────────────────────────────────────

async def esta_bloqueado(telefono: str) -> bool:
    async with async_session() as session:
        return await session.get(NumeroBloqueado, telefono) is not None


async def bloquear(telefono: str, motivo: str = "") -> None:
    async with async_session() as session:
        if not await session.get(NumeroBloqueado, telefono):
            session.add(NumeroBloqueado(telefono=telefono, motivo=motivo))
            await session.commit()
    logger.info(f"Bloqueado: {telefono}")


async def desbloquear(telefono: str) -> bool:
    async with async_session() as session:
        existente = await session.get(NumeroBloqueado, telefono)
        if not existente:
            return False
        await session.delete(existente)
        await session.commit()
    logger.info(f"Desbloqueado: {telefono}")
    return True


async def listar_bloqueados() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(NumeroBloqueado).order_by(NumeroBloqueado.creado_en.desc())
        )
        return [
            {
                "telefono": b.telefono,
                "motivo": b.motivo,
                "creado_en": b.creado_en.isoformat() + "Z",
            }
            for b in result.scalars().all()
        ]
