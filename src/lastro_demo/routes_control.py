"""Identidade (`/api/me`) e endpoints de controle do demo."""

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from lastro_demo.security import session_tenant
from lastro_demo.web import ConnDep, UserDep

router = APIRouter()


@router.get("/api/me")
def me(
    request: Request,
    user: UserDep,
    conn: ConnDep,
) -> Response:
    tenant = session_tenant(request, conn, user)
    if tenant is None:
        return JSONResponse(
            {"error": "tenant_not_selected", "message": "Selecione uma clínica."}, status_code=409
        )
    return JSONResponse(
        {
            "user": {"email": user.email, "name": user.display_name},
            "tenant": {"id": tenant.id, "name": tenant.name},
        }
    )
