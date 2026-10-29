"""Peças compartilhadas pelas rotas: templates, conexão por requisição e sessão."""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.responses import Response

from lastro_demo import db
from lastro_demo.runtime import DemoRuntime
from lastro_demo.security import (
    LoginRequired,
    Tenant,
    TenantRequired,
    User,
    session_tenant,
    session_user,
)
from lastro_demo.validation import ROLES, STATUSES

MSG_SAVED = "Funcionário salvo com sucesso."

templates = Jinja2Templates(
    env=Environment(
        loader=PackageLoader("lastro_demo", "templates"),
        autoescape=select_autoescape(default=True, default_for_string=True),
    )
)
templates.env.globals.update(roles=ROLES, statuses=STATUSES)


def get_runtime(request: Request) -> DemoRuntime:
    runtime: DemoRuntime = request.app.state.runtime
    return runtime


RuntimeDep = Annotated[DemoRuntime, Depends(get_runtime)]


def get_conn(runtime: RuntimeDep) -> Iterator[sqlite3.Connection]:
    with db.connection(runtime.config.db_path) as conn:
        yield conn


ConnDep = Annotated[sqlite3.Connection, Depends(get_conn)]


def current_user(
    request: Request,
    runtime: RuntimeDep,
    conn: ConnDep,
) -> User:
    user = session_user(request, conn, runtime.session_generation)
    if user is None:
        raise LoginRequired
    return user


UserDep = Annotated[User, Depends(current_user)]


@dataclass(frozen=True)
class TenantContext:
    user: User
    tenant: Tenant
    conn: sqlite3.Connection
    runtime: DemoRuntime


def tenant_context(
    request: Request,
    user: UserDep,
    conn: ConnDep,
    runtime: RuntimeDep,
) -> TenantContext:
    tenant = session_tenant(request, conn, user)
    if tenant is None:
        raise TenantRequired
    return TenantContext(user=user, tenant=tenant, conn=conn, runtime=runtime)


TenantDep = Annotated[TenantContext, Depends(tenant_context)]


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> Response:
    values: dict[str, Any] = {"user": None, "tenant": None, "flash": None}
    values.update(context or {})
    return templates.TemplateResponse(request, name, values, status_code=status_code)


async def form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}
