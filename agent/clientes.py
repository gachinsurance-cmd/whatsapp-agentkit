# agent/clientes.py — CRUD de clientes, plantillas y historial de recordatorios

import calendar
import csv
import io
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from agent.memory import (
    Cliente, HistorialRecordatorio, PlantillaRecordatorio, async_session,
)

logger = logging.getLogger("agentkit")

_TZ_CDMX = ZoneInfo("America/Mexico_City")

_PLANTILLAS_DEFAULT = [
    ("7_dias", "LamTV",
     "Hola *{nombre}*, buen día 👋\n"
     "Te informo que el servicio de tu usuario *{usuario_app}* de *LamTV* vence el *{fecha_vencimiento}* "
     "(en {dias_restantes} días). ¡Renueva a tiempo para no perder el servicio! "
     "Escríbenos cuando estés listo 🙌"),
    ("7_dias", "AztkPlay",
     "Hola *{nombre}*, buen día 👋\n"
     "Te informo que el servicio de tu usuario *{usuario_app}* de *AztkPlay* vence el *{fecha_vencimiento}* "
     "(en {dias_restantes} días). ¡Renueva antes para continuar sin interrupciones! "
     "Cualquier duda aquí estamos 🙌"),
    ("1_dia", "LamTV",
     "Hola *{nombre}* ⏰\n"
     "Mañana vence el servicio de tu usuario *{usuario_app}* de *LamTV* (*{fecha_vencimiento}*). "
     "¡No te quedes sin servicio! Escríbenos hoy y lo renovamos rápido 💪"),
    ("1_dia", "AztkPlay",
     "Hola *{nombre}* ⏰\n"
     "Mañana vence el servicio de tu usuario *{usuario_app}* de *AztkPlay* (*{fecha_vencimiento}*). "
     "¡Renueva hoy para no perder el acceso! Contáctanos y lo resolvemos en minutos 💪"),
    ("vencimiento", "LamTV",
     "Hola *{nombre}* 📅\n"
     "Hoy vence el servicio de tu usuario *{usuario_app}* de *LamTV*. "
     "Para seguir disfrutando sin cortes, renueva ahora. ¡Escríbenos y en minutos quedas activo! 🚀"),
    ("vencimiento", "AztkPlay",
     "Hola *{nombre}* 📅\n"
     "Hoy vence el servicio de tu usuario *{usuario_app}* de *AztkPlay*. "
     "Para no perder el acceso, activa hoy mismo. ¡Contáctanos y te activamos de inmediato! 🚀"),
]


# ── Utilidades ─────────────────────────────────────────────────────────────────

def calcular_vencimiento(activacion: date, meses: int) -> date:
    """Suma meses a una fecha manejando correctamente el fin de mes."""
    m = activacion.month - 1 + meses
    year = activacion.year + m // 12
    month = m % 12 + 1
    day = min(activacion.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _hoy_cdmx() -> date:
    return datetime.now(_TZ_CDMX).date()


def _cliente_dict(c: Cliente) -> dict:
    hoy = _hoy_cdmx()
    return {
        "id": c.id,
        "telefono": c.telefono,
        "nombre": c.nombre,
        "usuario_app": c.usuario_app,
        "producto": c.producto,
        "plan_meses": c.plan_meses,
        "fecha_activacion": c.fecha_activacion.isoformat(),
        "fecha_vencimiento": c.fecha_vencimiento.isoformat(),
        "dias_hasta_vencer": (c.fecha_vencimiento - hoy).days,
        "activo": c.activo,
        "recordatorios_pausados": c.recordatorios_pausados,
        "notas": c.notas,
        "creado_en": c.creado_en.isoformat() + "Z",
        "actualizado_en": c.actualizado_en.isoformat() + "Z",
    }


# ── Seed ───────────────────────────────────────────────────────────────────────

async def seed_plantillas() -> None:
    """
    Upserta las 6 plantillas. Si una ya existe pero no tiene {usuario_app},
    la actualiza al nuevo texto. Si ya fue personalizada con {usuario_app}, no toca nada.
    """
    async with async_session() as session:
        for tipo, producto, mensaje in _PLANTILLAS_DEFAULT:
            p = await session.get(PlantillaRecordatorio, (tipo, producto))
            if not p:
                session.add(PlantillaRecordatorio(tipo=tipo, producto=producto, mensaje=mensaje))
            elif "{usuario_app}" not in p.mensaje:
                p.mensaje = mensaje
        await session.commit()
    logger.info("Plantillas de recordatorio verificadas")


# ── Clientes CRUD ──────────────────────────────────────────────────────────────

async def listar_clientes(producto: str | None = None, dias_max: int | None = None) -> list[dict]:
    async with async_session() as session:
        q = select(Cliente).order_by(Cliente.fecha_vencimiento)
        result = await session.execute(q)
        clientes = result.scalars().all()

    hoy = _hoy_cdmx()
    out = []
    for c in clientes:
        dias = (c.fecha_vencimiento - hoy).days
        if producto and c.producto != producto:
            continue
        if dias_max is not None and dias > dias_max:
            continue
        out.append(_cliente_dict(c))
    return out


async def crear_cliente(
    nombre: str, telefono: str, producto: str, plan_meses: int,
    fecha_activacion: date, notas: str = "", usuario_app: str | None = None,
) -> dict:
    venc = calcular_vencimiento(fecha_activacion, plan_meses)
    async with async_session() as session:
        c = Cliente(
            telefono=telefono, nombre=nombre, producto=producto,
            plan_meses=plan_meses, fecha_activacion=fecha_activacion,
            fecha_vencimiento=venc, notas=notas, usuario_app=usuario_app or None,
        )
        session.add(c)
        await session.commit()
        await session.refresh(c)
        return _cliente_dict(c)


async def actualizar_cliente(cliente_id: int, cambios: dict) -> dict | None:
    async with async_session() as session:
        c = await session.get(Cliente, cliente_id)
        if not c:
            return None
        for campo, valor in cambios.items():
            setattr(c, campo, valor)
        # Recalcular vencimiento si cambió activación o plan
        if "fecha_activacion" in cambios or "plan_meses" in cambios:
            c.fecha_vencimiento = calcular_vencimiento(c.fecha_activacion, c.plan_meses)
        c.actualizado_en = datetime.utcnow()
        await session.commit()
        await session.refresh(c)
        return _cliente_dict(c)


async def borrar_cliente(cliente_id: int) -> bool:
    async with async_session() as session:
        c = await session.get(Cliente, cliente_id)
        if not c:
            return False
        await session.delete(c)
        await session.commit()
    return True


async def preparar_recordatorio_manual(cliente_id: int) -> dict | None:
    """
    Prepara el mensaje de recordatorio para envío manual.
    Retorna {telefono, nombre, tipo, mensaje} o None si no existe el cliente/plantilla.
    """
    hoy = _hoy_cdmx()
    async with async_session() as session:
        c = await session.get(Cliente, cliente_id)
        if not c:
            return None
        dias = (c.fecha_vencimiento - hoy).days
        if dias <= 0:
            tipo = "vencimiento"
        elif dias == 1:
            tipo = "1_dia"
        else:
            tipo = "7_dias"
        plantilla = await session.get(PlantillaRecordatorio, (tipo, c.producto))
        if not plantilla:
            return None
        try:
            mensaje = plantilla.mensaje.format(
                nombre=c.nombre,
                producto=c.producto,
                usuario_app=c.usuario_app or c.nombre,
                fecha_vencimiento=c.fecha_vencimiento.strftime("%d/%m/%Y"),
                dias_restantes=max(dias, 0),
            )
        except (KeyError, ValueError):
            return None
        return {"telefono": c.telefono, "nombre": c.nombre, "tipo": tipo, "mensaje": mensaje}


async def toggle_pausa(cliente_id: int) -> dict | None:
    async with async_session() as session:
        c = await session.get(Cliente, cliente_id)
        if not c:
            return None
        c.recordatorios_pausados = not c.recordatorios_pausados
        c.actualizado_en = datetime.utcnow()
        await session.commit()
        return {"id": c.id, "recordatorios_pausados": c.recordatorios_pausados}


async def importar_csv(datos: bytes) -> dict:
    """Upsert de clientes desde CSV. Retorna {insertados, actualizados, errores}."""
    for encoding in ("utf-8-sig", "utf-8", "latin-1", "cp1252"):
        try:
            text = datos.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("No se pudo decodificar el archivo CSV")
    # Detectar y normalizar separador
    if '\t' in text.split('\n')[0]:
        text = text.replace('\t', ',')
    reader = csv.DictReader(io.StringIO(text))
    requeridos = {"nombre", "telefono", "producto", "plan_meses", "fecha_activacion"}
    productos_validos = {"LamTV", "AztkPlay"}
    planes_validos = {1, 3, 6, 12}

    insertados = actualizados = 0
    errores: list[dict] = []

    for i, row in enumerate(reader, start=2):
        try:
            faltantes = requeridos - set(row.keys())
            if faltantes:
                errores.append({"fila": i, "error": f"Columnas faltantes: {faltantes}"})
                continue

            nombre = row["nombre"].strip()
            telefono = row["telefono"].strip()
            producto = row["producto"].strip()
            plan_meses = int(row["plan_meses"].strip())
            fecha_str = row["fecha_activacion"].strip()
            notas = row.get("notas", "").strip()
            usuario_app = row.get("usuario_app", "").strip() or None

            if not nombre or not telefono:
                errores.append({"fila": i, "error": "nombre y telefono son requeridos"})
                continue
            if producto not in productos_validos:
                errores.append({"fila": i, "error": f"producto inválido: '{producto}'. Usa LamTV o AztkPlay"})
                continue
            if plan_meses not in planes_validos:
                errores.append({"fila": i, "error": f"plan_meses inválido: {plan_meses}. Usa 1, 3, 6 o 12"})
                continue

            if '/' in fecha_str:
                partes = fecha_str.split('/')
                if len(partes[2]) == 4:  # DD/MM/YYYY
                    fecha_str = f"{partes[2]}-{partes[1].zfill(2)}-{partes[0].zfill(2)}"
                else:  # MM/DD/YY o similar
                    fecha_str = f"20{partes[2]}-{partes[0].zfill(2)}-{partes[1].zfill(2)}"
            fecha_act = date.fromisoformat(fecha_str)
            fecha_venc = calcular_vencimiento(fecha_act, plan_meses)

            async with async_session() as session:
                result = await session.execute(
                    select(Cliente).where(Cliente.telefono == telefono)
                )
                existente = result.scalars().first()
                if existente:
                    existente.nombre = nombre
                    existente.producto = producto
                    existente.plan_meses = plan_meses
                    existente.fecha_activacion = fecha_act
                    existente.fecha_vencimiento = fecha_venc
                    existente.notas = notas
                    existente.usuario_app = usuario_app
                    existente.actualizado_en = datetime.utcnow()
                    actualizados += 1
                else:
                    session.add(Cliente(
                        telefono=telefono, nombre=nombre, producto=producto,
                        plan_meses=plan_meses, fecha_activacion=fecha_act,
                        fecha_vencimiento=fecha_venc, notas=notas, usuario_app=usuario_app,
                    ))
                    insertados += 1
                await session.commit()

        except ValueError as e:
            errores.append({"fila": i, "error": str(e)})
        except Exception as e:
            errores.append({"fila": i, "error": f"Error: {e}"})

    return {"insertados": insertados, "actualizados": actualizados, "errores": errores}


# ── Plantillas ─────────────────────────────────────────────────────────────────

async def listar_plantillas() -> list[dict]:
    async with async_session() as session:
        result = await session.execute(
            select(PlantillaRecordatorio).order_by(
                PlantillaRecordatorio.tipo, PlantillaRecordatorio.producto
            )
        )
        return [
            {"tipo": p.tipo, "producto": p.producto, "mensaje": p.mensaje}
            for p in result.scalars().all()
        ]


async def actualizar_plantilla(tipo: str, producto: str, mensaje: str) -> bool:
    async with async_session() as session:
        p = await session.get(PlantillaRecordatorio, (tipo, producto))
        if not p:
            return False
        p.mensaje = mensaje
        await session.commit()
    return True


# ── Historial ──────────────────────────────────────────────────────────────────

async def listar_historial(cliente_id: int | None = None, limite: int = 50) -> list[dict]:
    async with async_session() as session:
        q = select(HistorialRecordatorio, Cliente.nombre).join(
            Cliente, HistorialRecordatorio.cliente_id == Cliente.id
        ).order_by(HistorialRecordatorio.enviado_en.desc()).limit(limite)
        if cliente_id:
            q = q.where(HistorialRecordatorio.cliente_id == cliente_id)
        result = await session.execute(q)
        return [
            {
                "id": h.id,
                "cliente_id": h.cliente_id,
                "nombre_cliente": nombre,
                "tipo": h.tipo,
                "enviado_en": h.enviado_en.isoformat() + "Z",
                "mensaje_enviado": h.mensaje_enviado,
            }
            for h, nombre in result.all()
        ]
