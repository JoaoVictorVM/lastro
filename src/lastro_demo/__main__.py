"""Modo isolado: `python -m lastro_demo` sobe o servidor do demo em primeiro plano."""

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import uvicorn

from lastro_demo import DEFAULT_HOST, DEFAULT_PORT
from lastro_demo.app import create_app
from lastro_demo.config import (
    HOST_ENV,
    PORT_ENV,
    SERVER_PID_FILENAME,
    SHUTDOWN_FILENAME,
    STATE_DIR_ENV,
    DemoConfig,
    announced_credential_env_vars,
)
from lastro_demo.flags import (
    DEFECT_IDS,
    SCENARIO_IDS,
    FlagState,
    UnknownFlagError,
    UnknownVariantError,
    parse_flag_list,
    validate_variant,
)

SHUTDOWN_POLL_SECONDS = 0.2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lastro_demo",
        description="Sobe o demo do Lastro em primeiro plano.",
    )
    parser.add_argument("--host", help=f"Host (padrão: {HOST_ENV} ou {DEFAULT_HOST}).")
    parser.add_argument("--port", type=int, help=f"Porta (padrão: {PORT_ENV} ou {DEFAULT_PORT}).")
    parser.add_argument(
        "--state-dir",
        type=Path,
        help=f"Pasta de banco e logs (padrão: {STATE_DIR_ENV} ou uma pasta temporária nova).",
    )
    parser.add_argument(
        "--defects", help="Defeitos ativos: lista separada por vírgula, all ou none (padrão: env)."
    )
    parser.add_argument(
        "--scenarios",
        help="Cenários ativos: lista separada por vírgula, all ou none (padrão: env).",
    )
    parser.add_argument("--variant", help="Variante dos defeitos (padrão: base).")
    return parser


def resolve_flags(args: argparse.Namespace) -> FlagState:
    flags = FlagState.from_env(os.environ)
    if args.defects is not None:
        flags = replace(flags, defects=parse_flag_list(args.defects, DEFECT_IDS))
    if args.scenarios is not None:
        flags = replace(flags, scenarios=parse_flag_list(args.scenarios, SCENARIO_IDS))
    if args.variant is not None:
        flags = replace(flags, variant=validate_variant(args.variant))
    return flags


async def serve(server: uvicorn.Server, shutdown_file: Path) -> None:
    """Serve até Ctrl+C ou até o runner pedir parada criando o arquivo de shutdown."""

    async def watch() -> None:
        while not server.should_exit:
            if shutdown_file.exists():
                server.should_exit = True
                return
            await asyncio.sleep(SHUTDOWN_POLL_SECONDS)

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        flags = resolve_flags(args)
    except (UnknownFlagError, UnknownVariantError) as exc:
        print(exc, file=sys.stderr)
        return 2
    config = DemoConfig.from_env(
        os.environ, state_dir=args.state_dir, host=args.host, port=args.port
    )
    shutdown_file = config.state_dir / SHUTDOWN_FILENAME
    config.state_dir.mkdir(parents=True, exist_ok=True)
    shutdown_file.unlink(missing_ok=True)
    pid_file = config.state_dir / SERVER_PID_FILENAME
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    app = create_app(config, flags)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            access_log=False,
            log_level="info",
            lifespan="off",
        )
    )
    print(f"Demo do Lastro em http://{config.host}:{config.port}", flush=True)
    print(f"Pasta de estado: {config.state_dir}", flush=True)
    print(f"Defeitos ativos: {', '.join(flags.sorted_defects()) or 'nenhum'}", flush=True)
    print(f"Cenários ativos: {', '.join(flags.sorted_scenarios()) or 'nenhum'}", flush=True)
    print(f"Credenciais: {', '.join(announced_credential_env_vars())}", flush=True)
    try:
        asyncio.run(serve(server, shutdown_file))
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_file.unlink(missing_ok=True)
        pid_file.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
