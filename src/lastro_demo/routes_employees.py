"""Páginas de funcionários: lista com busca e ordenação, formulários, detalhe e inativação."""

from datetime import date
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import Response

from lastro_demo import employees
from lastro_demo.employees import Employee, SaveOutcome
from lastro_demo.validation import EmployeeInput
from lastro_demo.web import MSG_SAVED, TenantContext, TenantDep, form_data, render

router = APIRouter()

MSG_NOT_FOUND = "Funcionário não encontrado."
MSG_DEACTIVATED = "Funcionário inativado."
EMPTY_FORM = {
    "full_name": "",
    "cpf": "",
    "email": "",
    "phone": "",
    "role": "",
    "hire_date": "",
    "status": "active",
}


def _base(ctx: TenantContext) -> dict[str, Any]:
    return {"user": ctx.user, "tenant": ctx.tenant}


def _not_found(request: Request, ctx: TenantContext) -> Response:
    return render(
        request, "not_found.html", {**_base(ctx), "message": MSG_NOT_FOUND}, status_code=404
    )


def _is_htmx_fragment(request: Request) -> bool:
    return (
        request.headers.get("HX-Request") == "true" and request.headers.get("HX-Boosted") != "true"
    )


@router.get("/employees")
def list_page(
    request: Request,
    ctx: TenantDep,
    q: str = "",
    sort: str | None = None,
    dir: str | None = None,
) -> Response:
    key, order = employees.normalize_sort(sort, dir)
    rows = employees.list_employees(
        ctx.conn, ctx.runtime, ctx.tenant.id, query=q, sort=key, direction=order
    )
    context = {**_base(ctx), "employees": rows, "q": q, "sort": key, "dir": order}
    template = "_employee_results.html" if _is_htmx_fragment(request) else "employees_list.html"
    return render(request, template, context)


def _form_page(
    request: Request,
    ctx: TenantContext,
    *,
    values: dict[str, str],
    errors: dict[str, str],
    employee: Employee | None,
    status_code: int = 200,
) -> Response:
    context: dict[str, Any] = {
        **_base(ctx),
        "values": values,
        "errors": errors,
        "employee": employee,
        "action": f"/employees/{employee.id}" if employee else "/employees",
        "show_deactivate": ctx.runtime.flags.scenario("s02"),
    }
    if employee is not None:
        context["name_literal"] = employees.script_name_literal(employee.full_name, ctx.runtime)
    return render(request, "employee_form.html", context, status_code=status_code)


def _saved_page(request: Request, ctx: TenantContext, outcome: SaveOutcome) -> Response:
    return render(
        request,
        "employee_detail.html",
        {**_base(ctx), "employee": outcome.employee, "flash": MSG_SAVED},
        status_code=outcome.status_code,
    )


@router.get("/employees/new")
def new_page(request: Request, ctx: TenantDep) -> Response:
    return _form_page(request, ctx, values=dict(EMPTY_FORM), errors={}, employee=None)


@router.post("/employees")
async def create(request: Request, ctx: TenantDep) -> Response:
    form = await form_data(request)
    data = EmployeeInput.from_form(form)
    outcome = employees.create_employee(
        ctx.conn, ctx.runtime, ctx.tenant.id, data, today=date.today()
    )
    if not outcome.ok:
        return _form_page(
            request,
            ctx,
            values=data.as_form(),
            errors=outcome.errors,
            employee=None,
            status_code=outcome.status_code,
        )
    return _saved_page(request, ctx, outcome)


@router.get("/employees/{employee_id}")
def detail_page(request: Request, employee_id: int, ctx: TenantDep) -> Response:
    employee = employees.get_employee(ctx.conn, ctx.tenant.id, employee_id)
    if employee is None:
        return _not_found(request, ctx)
    return render(request, "employee_detail.html", {**_base(ctx), "employee": employee})


@router.get("/employees/{employee_id}/edit")
def edit_page(request: Request, employee_id: int, ctx: TenantDep) -> Response:
    employee = employees.get_employee(ctx.conn, ctx.tenant.id, employee_id)
    if employee is None:
        return _not_found(request, ctx)
    return _form_page(request, ctx, values=employee.as_form(), errors={}, employee=employee)


@router.post("/employees/{employee_id}")
async def update(request: Request, employee_id: int, ctx: TenantDep) -> Response:
    current = employees.get_employee(ctx.conn, ctx.tenant.id, employee_id)
    if current is None:
        return _not_found(request, ctx)
    form = await form_data(request)
    data = EmployeeInput.from_form(form)
    outcome = employees.update_employee(
        ctx.conn, ctx.runtime, ctx.tenant.id, current, data, today=date.today()
    )
    if not outcome.ok:
        return _form_page(
            request,
            ctx,
            values=data.as_form(),
            errors=outcome.errors,
            employee=current,
            status_code=outcome.status_code,
        )
    return _saved_page(request, ctx, outcome)


@router.post("/employees/{employee_id}/deactivate")
def deactivate(request: Request, employee_id: int, ctx: TenantDep) -> Response:
    if not employees.deactivate_employee(ctx.conn, ctx.tenant.id, employee_id):
        return _not_found(request, ctx)
    employee = employees.get_employee(ctx.conn, ctx.tenant.id, employee_id)
    return render(
        request,
        "employee_detail.html",
        {**_base(ctx), "employee": employee, "flash": MSG_DEACTIVATED},
    )
