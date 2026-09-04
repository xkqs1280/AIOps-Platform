# -*- coding: utf-8 -*-
"""AI 辅助模块数据模型：接入配置、结果缓存、知识库、调用审计。"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AiSetting(Base):
    """AI 接入配置（全局单行）。api_key 使用 Fernet 加密存储。"""
    __tablename__ = "ai_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), default="ollama")  # ollama / gateway / cloud
    base_url: Mapped[str] = mapped_column(String(255), default="http://127.0.0.1:11434/v1")
    model: Mapped[str] = mapped_column(String(64), default="qwen2.5:7b-instruct")
    api_key: Mapped[str] = mapped_column(String(512), default="")  # Fernet 密文
    temperature: Mapped[int] = mapped_column(Integer, default=30)  # 0-100（/100 存储，避免 Float 精度问题）
    embed_model: Mapped[str] = mapped_column(String(64), default="nomic-embed-text")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AiCache(Base):
    """AI 场景结果缓存：同一场景同一目标 24 小时内复用，避免重复推理。"""
    __tablename__ = "ai_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(160), unique=True, index=True, nullable=False)
    scene: Mapped[str] = mapped_column(String(32), nullable=False)  # alert / backup / inspection / report
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiKbDoc(Base):
    """知识库文档。"""
    __tablename__ = "ai_kb_docs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="ready")  # processing / ready / failed
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiKbChunk(Base):
    """知识库分块 + 向量（embedding 以 JSON 数组存储，检索时纯 Python 计算余弦）。"""
    __tablename__ = "ai_kb_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("ai_kb_docs.id", ondelete="CASCADE"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list | None] = mapped_column(JSON)  # [float, ...]；None = 未向量化（降级关键词检索）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiLog(Base):
    """AI 调用审计日志。"""
    __tablename__ = "ai_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user: Mapped[str] = mapped_column(String(64), default="")
    scene: Mapped[str] = mapped_column(String(32), nullable=False)
    target: Mapped[str] = mapped_column(String(255), default="")
    provider: Mapped[str] = mapped_column(String(16), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    ok: Mapped[bool] = mapped_column(default=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
