"""
!!!!!! НЕ РЕДАКТИРОВАТЬ !!!!!!
"""

from fastapi import APIRouter, status

from aigw_service.context import APP_CTX

from . import schemas

router = APIRouter()


@router.get(
    "/health",
    status_code=status.HTTP_200_OK,
    response_model=schemas.HealthResponse,
)
async def health():
    return schemas.HealthResponse(status="running")


@router.get(
    "/info",
    status_code=status.HTTP_200_OK,
    response_model=schemas.InfoResponse,
)
async def info():
    return schemas.InfoResponse(**APP_CTX.app_metadata)
