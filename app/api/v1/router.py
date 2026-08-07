from fastapi import APIRouter
from app.api.v1.endpoints import pathfinding
from app.api.v1.endpoints import traffic

api_router = APIRouter()


api_router.include_router(pathfinding.router, prefix="/pathfinding", tags=["Pathfinding"])
# Daftarkan router traffic ke v1
api_router.include_router(traffic.router, prefix="/traffic", tags=["Traffic"])