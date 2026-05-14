# agent/memory.py — Memoria de conversaciones y estado del sistema

import os
from datetime import datetime, date
from typing import Optional
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import Boolean, Date, ForeignKey, String, Text, DateTime, select, Integer, text
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Escalacion(Base):
    __tablename__ = "escalaciones"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    razon: Mapped[str] = mapped_column(String(30), default="")
    ultimo_mensaje: Mapped[str] = mapped_column(Text, default="")
    escalado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ultimo_contacto: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FallaServicio(Base):
    __tablename__ = "fallas_servicio"

    servicio: Mapped[str] = mapped_column(String(50), primary_key=True)
    razon: Mapped[str] = mapped_column(Text, default="")
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resuelto_en: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class NumeroBloqueado(Base):
    __tablename__ = "numeros_bloqueados"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    motivo: Mapped[str] = mapped_column(Text, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Cliente(Base):
    __tablename__ = "clientes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    producto: Mapped[str] = mapped_column(String(10), nullable=False)   # LamTV | AztkPlay
    plan_meses: Mapped[int] = mapped_column(Integer, nullable=False)
    fecha_activacion: Mapped[date] = mapped_column(Date, nullable=False)
    fecha_vencimiento: Mapped[date] = mapped_column(Date, nullable=False)
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recordatorios_pausados: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    usuario_app: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    notas: Mapped[str] = mapped_column(Text, nullable=False, default="")
    creado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PlantillaRecordatorio(Base):
    __tablename__ = "plantillas_recordatorio"

    tipo: Mapped[str] = mapped_column(String(20), primary_key=True)     # 7_dias|1_dia|vencimiento
    producto: Mapped[str] = mapped_column(String(10), primary_key=True) # LamTV|AztkPlay
    mensaje: Mapped[str] = mapped_column(Text, nullable=False)


class HistorialRecordatorio(Base):
    __tablename__ = "historial_recordatorios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cliente_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("clientes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)
    enviado_en: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    mensaje_enviado: Mapped[str] = mapped_column(Text, nullable=False)


async def inicializar_db():
    """Crea todas las tablas si no existen y aplica migraciones incrementales."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Columnas añadidas tras el deploy inicial
    async with engine.begin() as conn:
        try:
            await conn.execute(text(
                "ALTER TABLE clientes ADD COLUMN IF NOT EXISTS usuario_app VARCHAR(100)"
            ))
        except Exception:
            pass  # columna ya existe (SQLite no soporta IF NOT EXISTS)


async def guardar_mensaje(telefono: str, role: str, content: str):
    async with async_session() as session:
        session.add(Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow(),
        ))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        mensajes.reverse()
        return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    async with async_session() as session:
        result = await session.execute(select(Mensaje).where(Mensaje.telefono == telefono))
        for m in result.scalars().all():
            await session.delete(m)
        await session.commit()
