# agent/admin_commands.py — Comandos WhatsApp del administrador

import os
import re
import logging

from agent.fallas import SERVICIOS_CONOCIDOS, obtener_fallas_activas, registrar_falla, resolver_falla

logger = logging.getLogger("agentkit")

ADMIN_PHONE = os.getenv("ADMIN_PHONE", "")

# #comando SERVICIO razón opcional
_CMD_RE = re.compile(r"^#(\w+)\s*(\S+)?\s*(.*)?$", re.IGNORECASE | re.DOTALL)


def es_comando_admin(telefono: str, texto: str) -> bool:
    """True si el mensaje es un comando admin (#) desde ADMIN_PHONE."""
    if not ADMIN_PHONE or telefono != ADMIN_PHONE:
        return False
    return texto.strip().startswith("#")


async def ejecutar_comando(texto: str, proveedor) -> str:
    """Ejecuta el comando y retorna la respuesta para enviar al admin."""
    m = _CMD_RE.match(texto.strip())
    if not m:
        return "Comando no reconocido.\nUsa: #falla SERVICIO [razón] | #resuelto SERVICIO | #estado"

    cmd = m.group(1).lower()
    arg_servicio = m.group(2) or ""
    razon = (m.group(3) or "").strip()

    if cmd == "falla":
        if not arg_servicio:
            return f"Uso: #falla SERVICIO [razón]\nServicios: {', '.join(sorted(SERVICIOS_CONOCIDOS))}"
        servicio = _match_servicio(arg_servicio)
        if not servicio:
            return f"Servicio no reconocido: '{arg_servicio}'\nServicios válidos: {', '.join(sorted(SERVICIOS_CONOCIDOS))}"
        await registrar_falla(servicio, razon)
        return f"⚠️ Falla registrada: {servicio}" + (f"\nRazón: {razon}" if razon else "")

    if cmd == "resuelto":
        if not arg_servicio:
            return f"Uso: #resuelto SERVICIO"
        servicio = _match_servicio(arg_servicio)
        if not servicio:
            return f"Servicio no reconocido: '{arg_servicio}'"
        resuelto = await resolver_falla(servicio)
        return f"✅ {servicio} marcado como resuelto." if resuelto else f"ℹ️ {servicio} no tenía falla activa."

    if cmd == "estado":
        fallas = obtener_fallas_activas()
        if not fallas:
            return "✅ Todos los servicios operando normalmente."
        lineas = [f"⚠️ {s}: {r}" if r else f"⚠️ {s}" for s, r in fallas.items()]
        return "Servicios con falla activa:\n" + "\n".join(lineas)

    return f"Comando desconocido: #{cmd}\nComandos: #falla | #resuelto | #estado"


def _match_servicio(arg: str) -> str | None:
    """Busca el servicio por nombre, case-insensitive. Retorna el nombre canónico o None."""
    arg_lower = arg.lower()
    return next((s for s in SERVICIOS_CONOCIDOS if s.lower() == arg_lower), None)
