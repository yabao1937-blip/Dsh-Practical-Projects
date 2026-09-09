"""重介密控系统后端（前后端分离，coal_records 统一主表）"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  确保建表前模型已注册
from .config import FRONTEND_DIR
from .database import Base, engine
from .routers import (health, import_api, inputs, manual_entries, migration, overview,
                      records, samples, settings, state, training)

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="重介密控系统",
    description="前后端分离后端：密度推荐 + 精煤灰分监控（coal_records 统一主表）",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api/v1")
app.include_router(migration.router, prefix="/api/v1")
app.include_router(settings.router, prefix="/api/v1")
app.include_router(inputs.router, prefix="/api/v1")
app.include_router(records.router, prefix="/api/v1")
app.include_router(samples.router, prefix="/api/v1")
app.include_router(manual_entries.router, prefix="/api/v1")
app.include_router(overview.router, prefix="/api/v1")
app.include_router(state.router, prefix="/api/v1")
app.include_router(import_api.router, prefix="/api/v1")
app.include_router(training.router, prefix="/api/v1")

# 静态托管前端（同源，规避 file:// 与 CORS）；挂在所有 API 路由之后
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
