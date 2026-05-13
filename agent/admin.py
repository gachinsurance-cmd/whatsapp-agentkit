# agent/admin.py — Panel de administración: archivos de knowledge

import os
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from agent.auth import ADMIN_USERNAME, create_token, verify_password, verify_token

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
