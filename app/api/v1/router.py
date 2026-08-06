from fastapi import APIRouter
from app.api.v1.endpoints import pathfinding  # Import endpoint pathfinding
from app.api.v1.endpoints import traffic  # Import endpoint traffic

api_router = APIRouter()

# Daftarkan router pathfinding ke v1
api_router.include_router(pathfinding.router, prefix="/pathfinding", tags=["Pathfinding"])
# Daftarkan router traffic ke v1
api_router.include_router(traffic.router, prefix="/traffic", tags=["Traffic"])