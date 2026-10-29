"""Login, seletor de clínica e logout."""

import sqlite3

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse, Response

from lastro_demo.security import (
    SESSION_TENANT,
    User,
    authenticate,
    load_tenant,
    session_tenant,
    session_user,
    start_session,
)
from lastro_demo.web import ConnDep, RuntimeDep, UserDep, form_data, render

router = APIRouter()

MSG_LOGIN_FAILED = "Usuário ou senha inválidos."
MSG_CLINIC_DENIED = "Você não tem acesso a esta clínica."


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/")
def home(
    request: Request,
    runtime: RuntimeDep,
    conn: ConnDep,
) -> Response:
    user = session_user(request, conn, runtime.session_generation)
    return _see_other("/employees" if user is not None else "/login")


@router.get("/login")
def login_page(
    request: Request,
    runtime: RuntimeDep,
    conn: ConnDep,
) -> Response:
    user = session_user(request, conn, runtime.session_generation)
    if user is not None:
        return _see_other("/employees")
    return render(request, "login.html", {"username": ""})


@router.post("/login")
async def login(
    request: Request,
    runtime: RuntimeDep,
    conn: ConnDep,
) -> Response:
    form = await form_data(request)
    username = form.get("username", "").strip()
    user = authenticate(conn, username, form.get("password", ""))
    if user is None:
        request.session.clear()
        return render(request, "login.html", {"username": username, "error": MSG_LOGIN_FAILED})
    start_session(request, user, runtime.session_generation)
    if len(user.clinics) == 1:
        return _see_other("/employees")
    return _see_other("/select-clinic")


def _clinic_choices(conn: sqlite3.Connection, user: User) -> list[dict[str, str]]:
    choices: list[dict[str, str]] = []
    for clinic_id in user.clinics:
        tenant = load_tenant(conn, clinic_id)
        if tenant is not None:
            choices.append({"id": tenant.id, "name": tenant.name})
    return choices


@router.get("/select-clinic")
def select_clinic_page(
    request: Request,
    user: UserDep,
    conn: ConnDep,
) -> Response:
    tenant = session_tenant(request, conn, user)
    return render(
        request,
        "select_clinic.html",
        {"user": user, "tenant": tenant, "clinics": _clinic_choices(conn, user)},
    )


@router.post("/select-clinic")
async def select_clinic(
    request: Request,
    user: UserDep,
    conn: ConnDep,
) -> Response:
    form = await form_data(request)
    choice = form.get("tenant", "").strip()
    if choice not in user.clinics:
        return render(
            request,
            "select_clinic.html",
            {
                "user": user,
                "tenant": session_tenant(request, conn, user),
                "clinics": _clinic_choices(conn, user),
                "error": MSG_CLINIC_DENIED,
            },
            status_code=403,
        )
    request.session[SESSION_TENANT] = choice
    return _see_other("/employees")


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return _see_other("/login")
