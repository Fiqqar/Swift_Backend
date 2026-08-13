from fastapi import APIRouter
from app.api.v1.endpoints import pathfinding
from app.api.v1.endpoints import traffic
from app.api.v1.endpoints import auth
from app.api.v1.endpoints import shipments

api_router = APIRouter()


api_router.include_router(pathfinding.router, prefix="/pathfinding", tags=["Pathfinding"])

api_router.include_router(traffic.router, prefix="/traffic", tags=["Traffic"])

api_router.include_router(auth.router, tags=["Auth"])

api_router.include_router(shipments.router, tags=["Shipment"])