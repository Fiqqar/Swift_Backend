from fastapi import APIRouter
from app.api.v1.endpoints import pathfinding # Import endpoint baru

api_router = APIRouter()

# Daftarkan router pathfinding ke v1
api_router.include_router(pathfinding.router, prefix="/pathfinding", tags=["Pathfinding"])