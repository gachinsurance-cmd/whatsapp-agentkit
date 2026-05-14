# agent/admin.py — Panel de administración: archivos de knowledge

import os
import logging
from datetime import date as DateType, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from agent.auth import ADMIN_USERNAME, create_token, verify_password, verify_token
from agent.clientes import (
    actualizar_cliente, actualizar_plantilla, borrar_cliente, crear_cliente,
    importar_csv, listar_clientes, listar_historial, listar_plantillas, toggle_pausa,
)
from agent.escalation import (
    bloquear, desbloquear, desescalar, listar_bloqueados, listar_escalaciones,
)

logger = logging.getLogger("agentkit")

KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "knowledge")

_COOKIE_NAME = "admin_token"
_ALLOWED_EXTENSIONS = {"pdf", "txt", "md", "csv", "json", "yaml", "yml"}
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
_TEXT_EXTENSIONS = {"txt", "md", "csv", "json", "yaml", "yml"}

# ── Helpers ────────────────────────────────────────────────────────────────────

def _cookie_secure() -> bool:
    return os.getenv("ENVIRONMENT", "development") == "production"


def _safe_path(filename: str) -> Path:
    """
    Resuelve la ruta del archivo y verifica que esté dentro de KNOWLEDGE_DIR.
    Lanza 400 si el nombre es inválido, 403 si intenta salir del directorio.
    """
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")

    base = Path(KNOWLEDGE_DIR).resolve()
    target = (base / filename).resolve()

    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=403, detail="Acceso denegado")

    return target


def _require_auth(request: Request) -> None:
    """Lanza 401 si el JWT de la cookie no es válido."""
    token = request.cookies.get(_COOKIE_NAME)
    if not token or not verify_token(token):
        raise HTTPException(status_code=401, detail="No autorizado")


# ── Router ─────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/admin", tags=["admin"])

_TEMPLATE = Path(__file__).parent / "templates" / "panel.html"


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def panel():
    """Sirve el panel de administración (SPA — el JS maneja auth)."""
    return HTMLResponse(content=_TEMPLATE.read_text(encoding="utf-8"))


@router.get("/me")
async def me(request: Request):
    """Verifica si la sesión actual es válida. Usado por el frontend al cargar."""
    _require_auth(request)
    return {"username": ADMIN_USERNAME}


# ── Auth ───────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    if body.username != ADMIN_USERNAME or not verify_password(body.password):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")

    token = create_token()
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        max_age=86400,  # 24h en segundos
    )
    logger.info("Admin login exitoso")
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=_COOKIE_NAME)
    return {"ok": True}


# ── Archivos ───────────────────────────────────────────────────────────────────

@router.get("/files")
async def list_files(request: Request):
    """Lista todos los archivos en KNOWLEDGE_DIR con nombre, tamaño y mtime."""
    _require_auth(request)

    base = Path(KNOWLEDGE_DIR)
    if not base.exists():
        return {"files": []}

    files = []
    for entry in sorted(base.iterdir()):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        stat = entry.stat()
        files.append({
            "name": entry.name,
            "size": stat.st_size,
            "mtime": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
            "type": "text" if entry.suffix.lstrip(".").lower() in _TEXT_EXTENSIONS else "binary",
        })

    return {"files": files}


@router.get("/files/{filename}")
async def get_file(filename: str, request: Request):
    """
    Devuelve el contenido del archivo.
    - Archivos de texto: JSON con {content: "..."}
    - PDFs y binarios: FileResponse para descarga/visualización directa
    """
    _require_auth(request)

    path = _safe_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in _TEXT_EXTENSIONS:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"No se pudo leer el archivo: {e}")
        return {"name": filename, "content": content}

    return FileResponse(path=str(path), filename=filename)


class UpdateRequest(BaseModel):
    content: str


@router.put("/files/{filename}")
async def update_file(filename: str, body: UpdateRequest, request: Request):
    """Sobreescribe el contenido de un archivo de texto existente."""
    _require_auth(request)

    path = _safe_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _TEXT_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Solo se pueden editar archivos de texto")

    try:
        path.write_text(body.content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar: {e}")

    logger.info(f"Admin actualizó archivo: {filename}")
    return {"ok": True}


@router.post("/files")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Sube un archivo nuevo a KNOWLEDGE_DIR."""
    _require_auth(request)

    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="Nombre de archivo requerido")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Extensión no permitida. Permitidas: {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
        )

    path = _safe_path(filename)

    data = await file.read()
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Archivo demasiado grande (máximo 10 MB)")

    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    try:
        path.write_bytes(data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar: {e}")

    logger.info(f"Admin subió archivo: {filename} ({len(data)} bytes)")
    return {"ok": True, "name": filename, "size": len(data)}


@router.delete("/files/{filename}")
async def delete_file(filename: str, request: Request):
    """Elimina un archivo de KNOWLEDGE_DIR."""
    _require_auth(request)

    path = _safe_path(filename)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    try:
        path.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo eliminar: {e}")

    logger.info(f"Admin eliminó archivo: {filename}")
    return {"ok": True}


# ── Escalaciones ───────────────────────────────────────────────────────────────

@router.get("/escalaciones")
async def get_escalaciones(request: Request):
    """Lista los clientes actualmente escalados."""
    _require_auth(request)
    return {"escalaciones": await listar_escalaciones()}


@router.delete("/escalaciones/{telefono}")
async def delete_escalacion(telefono: str, request: Request):
    """Desescala manualmente un cliente desde el panel admin."""
    _require_auth(request)
    fue_desescalado = await desescalar(telefono)
    if not fue_desescalado:
        raise HTTPException(status_code=404, detail="Cliente no está escalado")
    logger.info(f"Admin desescaló manualmente: {telefono}")
    return {"ok": True}


# ── Lista negra ────────────────────────────────────────────────────────────────

class BloqueadoRequest(BaseModel):
    telefono: str
    motivo: str = ""


@router.get("/bloqueados")
async def get_bloqueados(request: Request):
    """Lista los números en la lista negra."""
    _require_auth(request)
    return {"bloqueados": await listar_bloqueados()}


@router.post("/bloqueados")
async def add_bloqueado(body: BloqueadoRequest, request: Request):
    """Agrega un número a la lista negra."""
    _require_auth(request)
    telefono = body.telefono.strip()
    if not telefono:
        raise HTTPException(status_code=400, detail="Teléfono requerido")
    await bloquear(telefono, body.motivo.strip())
    logger.info(f"Admin bloqueó: {telefono}")
    return {"ok": True}


@router.delete("/bloqueados/{telefono}")
async def remove_bloqueado(telefono: str, request: Request):
    """Quita un número de la lista negra."""
    _require_auth(request)
    fue_desbloqueado = await desbloquear(telefono)
    if not fue_desbloqueado:
        raise HTTPException(status_code=404, detail="Número no está en la lista negra")
    logger.info(f"Admin desbloqueó: {telefono}")
    return {"ok": True}


# ── Clientes ───────────────────────────────────────────────────────────────────

_PRODUCTOS_VALIDOS = {"LamTV", "AztkPlay"}
_PLANES_VALIDOS = {1, 3, 6, 12}
_TIPOS_VALIDOS = {"7_dias", "1_dia", "vencimiento"}


class ClienteCreate(BaseModel):
    nombre: str
    telefono: str
    producto: str
    plan_meses: int
    fecha_activacion: DateType
    usuario_app: Optional[str] = None
    notas: str = ""


class ClienteUpdate(BaseModel):
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    producto: Optional[str] = None
    plan_meses: Optional[int] = None
    fecha_activacion: Optional[DateType] = None
    activo: Optional[bool] = None
    usuario_app: Optional[str] = None
    notas: Optional[str] = None


class PlantillaUpdate(BaseModel):
    mensaje: str


def _validar_cliente(producto: str, plan_meses: int) -> None:
    if producto not in _PRODUCTOS_VALIDOS:
        raise HTTPException(status_code=400, detail=f"producto inválido. Usa: {_PRODUCTOS_VALIDOS}")
    if plan_meses not in _PLANES_VALIDOS:
        raise HTTPException(status_code=400, detail=f"plan_meses inválido. Usa: {_PLANES_VALIDOS}")


@router.get("/clientes")
async def get_clientes(
    request: Request,
    producto: Optional[str] = Query(None),
    dias_max: Optional[int] = Query(None),
):
    _require_auth(request)
    return {"clientes": await listar_clientes(producto=producto, dias_max=dias_max)}


@router.post("/clientes")
async def post_cliente(body: ClienteCreate, request: Request):
    _require_auth(request)
    _validar_cliente(body.producto, body.plan_meses)
    try:
        cliente = await crear_cliente(
            nombre=body.nombre.strip(),
            telefono=body.telefono.strip(),
            producto=body.producto,
            plan_meses=body.plan_meses,
            fecha_activacion=body.fecha_activacion,
            usuario_app=body.usuario_app.strip() if body.usuario_app else None,
            notas=body.notas.strip(),
        )
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="El teléfono ya existe")
        raise HTTPException(status_code=500, detail=str(e))
    logger.info(f"Admin creó cliente: {body.nombre} ({body.telefono})")
    return cliente


@router.put("/clientes/{cliente_id}")
async def put_cliente(cliente_id: int, body: ClienteUpdate, request: Request):
    _require_auth(request)
    cambios = {k: v for k, v in body.model_dump().items() if v is not None}
    if "producto" in cambios:
        _validar_cliente(cambios["producto"], cambios.get("plan_meses", 1))
    cliente = await actualizar_cliente(cliente_id, cambios)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    logger.info(f"Admin actualizó cliente id={cliente_id}")
    return cliente


@router.delete("/clientes/{cliente_id}")
async def del_cliente(cliente_id: int, request: Request):
    _require_auth(request)
    if not await borrar_cliente(cliente_id):
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    logger.info(f"Admin eliminó cliente id={cliente_id}")
    return {"ok": True}


# Ruta fija ANTES del patrón /{id}/pausar para que FastAPI no confunda
@router.post("/clientes/importar")
async def importar_clientes(request: Request, file: UploadFile = File(...)):
    _require_auth(request)
    if not (file.filename or "").endswith(".csv"):
        raise HTTPException(status_code=400, detail="Se requiere un archivo .csv")
    datos = await file.read()
    if len(datos) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="CSV demasiado grande (máx 5 MB)")
    resultado = await importar_csv(datos)
    logger.info(f"Import CSV: {resultado['insertados']} insertados, {resultado['actualizados']} actualizados")
    return resultado


@router.post("/clientes/{cliente_id}/pausar")
async def pausar_cliente(cliente_id: int, request: Request):
    _require_auth(request)
    estado = await toggle_pausa(cliente_id)
    if not estado:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return estado


# ── Plantillas ─────────────────────────────────────────────────────────────────

@router.get("/plantillas")
async def get_plantillas(request: Request):
    _require_auth(request)
    return {"plantillas": await listar_plantillas()}


@router.put("/plantillas/{tipo}/{producto}")
async def put_plantilla(tipo: str, producto: str, body: PlantillaUpdate, request: Request):
    _require_auth(request)
    if tipo not in _TIPOS_VALIDOS:
        raise HTTPException(status_code=400, detail=f"tipo inválido. Usa: {_TIPOS_VALIDOS}")
    if producto not in _PRODUCTOS_VALIDOS:
        raise HTTPException(status_code=400, detail=f"producto inválido. Usa: {_PRODUCTOS_VALIDOS}")
    if not await actualizar_plantilla(tipo, producto, body.mensaje.strip()):
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    logger.info(f"Admin actualizó plantilla {tipo}/{producto}")
    return {"ok": True}


# ── Historial ──────────────────────────────────────────────────────────────────

@router.get("/historial")
async def get_historial(
    request: Request,
    cliente_id: Optional[int] = Query(None),
    limite: int = Query(50, le=200),
):
    _require_auth(request)
    return {"historial": await listar_historial(cliente_id=cliente_id, limite=limite)}
