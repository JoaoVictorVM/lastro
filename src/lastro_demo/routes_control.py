"""Identidade (`/api/me`) e endpoints de controle do demo (`/__demo/*`, só loopback)."""

import ipaddress
import json
from typing import Any

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response

from lastro_demo import __version__
from lastro_demo.flags import (
    FlagState,
    UnknownFlagError,
    UnknownVariantError,
    validate_defects,
    validate_scenarios,
    validate_variant,
)
from lastro_demo.runtime import DemoRuntime
from lastro_demo.security import session_tenant
from lastro_demo.web import ConnDep, RuntimeDep, UserDep

router = APIRouter()

RESET_KEYS = frozenset({"defects", "scenarios", "variant"})


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


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _error(code: str, message: str, status_code: int = 422) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status_code)


def _forbidden() -> JSONResponse:
    return _error("forbidden", "Endpoint de controle disponível só via loopback.", 403)


def _flag_payload(runtime: DemoRuntime) -> dict[str, Any]:
    return {
        "variant": runtime.flags.variant,
        "defects": runtime.flags.sorted_defects(),
        "scenarios": runtime.flags.sorted_scenarios(),
    }


@router.get("/__demo/version")
def version(request: Request, runtime: RuntimeDep) -> Response:
    if not is_loopback(request):
        return _forbidden()
    return JSONResponse({"version": __version__, **_flag_payload(runtime)})


def _string_list(payload: dict[str, Any], key: str) -> list[str] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"'{key}' deve ser uma lista de strings.")
    return [str(item) for item in value]


def _requested_flags(payload: dict[str, Any], current: FlagState) -> FlagState:
    """Chave ausente mantém o valor atual; presente substitui o conjunto inteiro."""
    defects = _string_list(payload, "defects")
    scenarios = _string_list(payload, "scenarios")
    variant = payload.get("variant")
    if variant is not None and not isinstance(variant, str):
        raise ValueError("'variant' deve ser uma string.")
    return FlagState(
        defects=current.defects if defects is None else validate_defects(defects),
        scenarios=current.scenarios if scenarios is None else validate_scenarios(scenarios),
        variant=current.variant if variant is None else validate_variant(variant),
    )


@router.post("/__demo/reset")
async def reset(request: Request, runtime: RuntimeDep) -> Response:
    if not is_loopback(request):
        return _forbidden()
    raw = await request.body()
    try:
        payload: Any = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("invalid_body", "Corpo deve ser JSON.")
    if not isinstance(payload, dict):
        return _error("invalid_body", "Corpo deve ser um objeto JSON.")
    body: dict[str, Any] = payload
    unknown_keys = sorted(set(body) - RESET_KEYS)
    if unknown_keys:
        return _error("invalid_body", f"Campos desconhecidos: {', '.join(unknown_keys)}.")
    try:
        requested = _requested_flags(body, runtime.flags)
    except UnknownFlagError as exc:
        return JSONResponse(
            {"error": "unknown_flag", "message": str(exc), "flags": list(exc.unknown)},
            status_code=422,
        )
    except UnknownVariantError as exc:
        return _error("unknown_variant", str(exc))
    except ValueError as exc:
        return _error("invalid_body", str(exc))
    outcome = runtime.reset(requested)
    return JSONResponse(
        {
            "status": "ok",
            "clinics": outcome.clinics,
            "employees": outcome.employees,
            **_flag_payload(runtime),
            "duration_ms": outcome.duration_ms,
        }
    )
