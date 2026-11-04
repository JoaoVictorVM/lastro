"""Preflight, recursos, execução e limpeza do executor nativo.

A run só é criada depois do preflight; a partir daí todo término passa pelo encerramento
confiável do browser owner, ou, sem ele, pelo encerramento local depois de o filho morrer.
"""

import asyncio
import contextlib
import importlib.metadata
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, TextIO, cast

from lastro.browser.control import request_termination
from lastro.config.loader import ProjectContext
from lastro.config.native import NativeSettings
from lastro.history import (
    HistoryError,
    HistoryStore,
    JsonValue,
    NativeContextInput,
    RunStatus,
)
from lastro.mission.bundle import BundleError
from lastro.native import MISSING_EXTRA_MESSAGE, missing_dependencies
from lastro.native.budget import BudgetLedger
from lastro.native.human import HumanDecisionPort
from lastro.native.mcp import (
    CatalogTool,
    McpSessionAdapter,
    catalog_sha256,
    compare_catalog,
    expected_catalog,
)
from lastro.native.models import (
    GRAPH_RECURSION_LIMIT,
    NativeOutcome,
    NativeState,
    UsageSummary,
)
from lastro.native.pricing import document_sha256, format_usd, pricing_document, to_nusd
from lastro.native.prompts import Bundle, load_bundle, system_blocks
from lastro.native.provider import (
    API_KEY_MESSAGE,
    ApiAuthError,
    ApiUnavailableError,
    ModelGateway,
    ModelRequest,
    Provider,
    count_tokens,
)

API_KEY_ENV = "ANTHROPIC_API_KEY"
TERMINATION_TIMEOUT_SECONDS = 60.0

type Writer = Callable[[str], None]
type ProviderFactory = Callable[[str], Provider]


class PreflightError(Exception):
    """Falha antes de criar a run: nenhuma run, nenhum browser, código de saída 2."""


@dataclass(frozen=True)
class Preflight:
    settings: NativeSettings
    api_key: str
    bundle: Bundle


def default_provider(api_key: str) -> Provider:
    from lastro.native.provider import AnthropicProvider

    return AnthropicProvider(api_key)


# Ponto de injeção dos testes: provedor roteirizado no lugar da API real.
PROVIDER_FACTORY: ProviderFactory = default_provider


def probe_request(model: str) -> ModelRequest:
    return ModelRequest(
        model=model,
        system=[{"type": "text", "text": "Lastro native executor preflight."}],
        messages=[{"role": "user", "content": "ping"}],
        tools=[],
        max_tokens=1,
    )


async def preflight(
    project: ProjectContext,
    provider_factory: ProviderFactory | None = None,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[Preflight, Provider]:
    """Extra, chave, modelos e bundle conferidos; a chave é validada só por contagem de tokens."""
    if missing_dependencies():
        raise PreflightError(MISSING_EXTRA_MESSAGE)
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise PreflightError(API_KEY_MESSAGE)
    settings = project.profile.native
    try:
        bundle = load_bundle(project.loaded.project_dir)
    except BundleError as error:
        raise PreflightError(str(error)) from None
    provider = (provider_factory or PROVIDER_FACTORY)(api_key)
    for model in sorted(settings.models()):
        try:
            await count_tokens(provider, probe_request(model), sleep=sleep)
        except ApiAuthError:
            raise PreflightError(API_KEY_MESSAGE) from None
        except ApiUnavailableError as error:
            raise PreflightError(
                f"Anthropic API unavailable during preflight ({error.code}); the key was not "
                "rejected and no run was created. Try again later."
            ) from None
    return Preflight(settings=settings, api_key=api_key, bundle=bundle), provider


def runtime_versions() -> dict[str, Any]:
    versions: dict[str, Any] = {"python": sys.version.split()[0]}
    for name in ("lastro", "langgraph", "anthropic", "mcp"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def usage_summary(history: HistoryStore, run_id: str) -> UsageSummary | None:
    usage = history.native_usage(run_id)
    if usage is None:
        return None
    return UsageSummary(
        calls=usage.calls,
        uncertain_calls=usage.uncertain_calls,
        input_tokens=usage.total_input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        confirmed_usd=format_usd(usage.confirmed_nusd),
        committed_usd=format_usd(usage.committed_nusd),
    )


def summary_lines(outcome: NativeOutcome) -> list[str]:
    reason = f" ({outcome.reason})" if outcome.reason else ""
    lines = [f"Run {outcome.run_id}: {outcome.status}{reason}"]
    lines += [f"Report: {path}" for path in outcome.report_paths]
    if outcome.report_status is not None and outcome.report_status != "ready":
        lines.append(f"Report status: {outcome.report_status}")
    usage = outcome.usage
    if usage is None:
        lines.append("Tokens: no model call recorded")
        return lines
    lines.append(
        f"Tokens: input {usage.input_tokens} (cache read {usage.cache_read_tokens}, "
        f"cache write {usage.cache_write_tokens}), output {usage.output_tokens}"
    )
    lines.append(f"Cost: USD {usage.confirmed_usd} confirmed over {usage.calls} model calls")
    if usage.uncertain_calls:
        lines.append(
            f"Committed (conservative): USD {usage.committed_usd}; "
            f"{usage.uncertain_calls} attempt(s) with unknown usage, not counted as zero"
        )
    return lines


def default_gateway(provider: Provider, ledger: BudgetLedger) -> ModelGateway:
    return ModelGateway(provider, ledger)


class NativeRunner:
    def __init__(
        self,
        project: ProjectContext,
        history: HistoryStore,
        run_id: str,
        prepared: Preflight,
        provider: Provider,
        *,
        write: Writer,
        interactive: bool,
        suite_budget_id: str | None = None,
        human: HumanDecisionPort | None = None,
        gateway_factory: Callable[[Provider, BudgetLedger], ModelGateway] | None = None,
    ) -> None:
        self.project, self.history, self.run_id = project, history, run_id
        self.prepared, self.provider = prepared, provider
        self.write = write
        self.suite_budget_id = suite_budget_id
        self.human = human or HumanDecisionPort(
            history=history,
            run_id=run_id,
            write=write,
            interactive=interactive,
            timezone=project.timezone,
        )
        self.gateway_factory = gateway_factory or default_gateway
        self.adapter: McpSessionAdapter | None = None
        self.errlog: TextIO | None = None
        self.outcome: NativeOutcome | None = None

    async def run(self) -> NativeOutcome:
        try:
            return await self._run()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            # Ctrl+C: asyncio.run cancela a task; o encerramento termina antes de propagar.
            await self.finish("cancelled", "user_cancelled")
            raise
        finally:
            await self.close()

    async def _run(self) -> NativeOutcome:
        record = self.history.get_run(self.run_id)
        settings = self.prepared.settings
        if record.skill_bundle_version != self.prepared.bundle.sha256:
            return await self.finish("blocked", "executor_configuration")
        self.errlog = (self.history.run_dir(self.run_id) / "native-mcp.log").open(
            "a", encoding="utf-8"
        )
        self.adapter = McpSessionAdapter(
            project_dir=self.project.loaded.project_dir,
            config_path=self.project.config_path,
            profile=self.project.profile_name,
            run_id=self.run_id,
            errlog=self.errlog,
        )
        try:
            catalog = await self.adapter.open()
        except Exception:
            self.write("! MCP server did not start; see native-mcp.log in the run folder.")
            return await self.finish("blocked", "executor_configuration")
        problems = compare_catalog(catalog, expected_catalog())
        if problems:
            self.write("! tool catalog differs from the Lastro contract: " + "; ".join(problems))
            return await self.finish("blocked", "executor_configuration")
        pricing = pricing_document(settings.models())
        pricing_sha = document_sha256(pricing)
        try:
            self.history.start_native_run(
                NativeContextInput(
                    run_id=self.run_id,
                    cap_nusd=to_nusd(Decimal(settings.cost_cap_usd)),
                    suite_budget_id=self.suite_budget_id,
                    catalog=[tool.document() for tool in catalog],
                    catalog_sha256=catalog_sha256(catalog),
                    pricing=cast(JsonValue, pricing),
                    pricing_sha256=pricing_sha,
                    runtime_versions=runtime_versions(),
                )
            )
        except HistoryError:
            return await self.finish("blocked", "executor_configuration")
        self.write(
            f"Executor: native · model {settings.model} · cost cap USD {settings.cost_cap_usd}"
            f" · catalog {catalog_sha256(catalog)[:12]} · bundle {self.prepared.bundle.sha256[:12]}"
        )
        return await self.loop(catalog, pricing_sha)

    async def loop(self, catalog: list[CatalogTool], pricing_sha: str) -> NativeOutcome:
        from langgraph.errors import GraphRecursionError

        from lastro.native.graph import NativeAgent, build_graph

        assert self.adapter is not None
        ledger = BudgetLedger(self.history, self.run_id, pricing_id="sha256:" + pricing_sha)
        agent = NativeAgent(
            history=self.history,
            run_id=self.run_id,
            adapter=self.adapter,
            gateway=self.gateway_factory(self.provider, ledger),
            human=self.human,
            settings=self.prepared.settings,
            system=system_blocks(self.prepared.bundle),
            write=self.write,
            tools=[tool.document() for tool in catalog],
        )

        async def wrap_up(state: NativeState) -> NativeState:
            ended = state.get("ended_by_agent")
            if ended is not None:
                await self.from_agent_end(ended)
            else:
                status, reason = state.get("terminal") or ("interrupted", "graph_limit")
                await self.finish(status, reason)
            return state

        graph = build_graph(agent, wrap_up)
        try:
            await graph.ainvoke({}, config={"recursion_limit": GRAPH_RECURSION_LIMIT})
        except GraphRecursionError:
            await self.finish("interrupted", "graph_limit")
        if self.outcome is None:
            await self.finish("interrupted", "graph_limit")
        assert self.outcome is not None
        return self.outcome

    async def from_agent_end(self, data: dict[str, Any]) -> None:
        """end_run do modelo já finalizou no owner; o resumo lê o estado durável."""
        record = self.history.get_run(self.run_id)
        paths = [str(data["report_path"])] if data.get("report_path") else []
        self.outcome = NativeOutcome(
            run_id=self.run_id,
            status=record.status.value,
            reason=record.status_reason,
            report_paths=paths,
            report_status=str(data.get("report_status")) if data.get("report_status") else None,
            usage=usage_summary(self.history, self.run_id),
        )

    async def finish(self, status: str, reason: str) -> NativeOutcome:
        """Encerramento sem nova geração: owner confiável, ou local depois de o filho morrer."""
        if self.outcome is not None:
            return self.outcome
        record = self.history.get_run(self.run_id)
        paths: list[str] = []
        report_status: str | None = None
        if not record.status.is_terminal and self.adapter is not None and not self.adapter.lost:
            try:
                response = await asyncio.to_thread(
                    request_termination,
                    self.project.project_data_dir,
                    self.run_id,
                    status,
                    reason,
                    TERMINATION_TIMEOUT_SECONDS,
                )
                if response.get("report_path"):
                    paths.append(str(response["report_path"]))
                report_status = _text(response.get("report_status"))
            except (OSError, ValueError):
                self.write("! browser owner unavailable; closing it before finishing locally.")
        record = self.history.get_run(self.run_id)
        if not record.status.is_terminal:
            await self.close()
            paths, report_status = self.finish_locally(status, reason)
        elif not paths:
            paths, report_status = self.existing_report(record.status)
        record = self.history.get_run(self.run_id)
        self.outcome = NativeOutcome(
            run_id=self.run_id,
            status=record.status.value,
            reason=record.status_reason,
            report_paths=paths,
            report_status=report_status,
            usage=usage_summary(self.history, self.run_id),
        )
        return self.outcome

    def finish_locally(self, status: str, reason: str) -> tuple[list[str], str | None]:
        from lastro.reporting.builder import ReportBuilder

        target = RunStatus(status)
        with contextlib.suppress(HistoryError):
            self.history.seal_criterion_results(self.run_id)
        self.history.finish_run(self.run_id, target, reason)
        try:
            report = ReportBuilder(self.history).build_partial(self.run_id, reason)
        except Exception as error:
            self.write(f"! report failed: {error}")
            return [], "failed"
        path = report.report_path or report.fallback_path
        return ([str(path)] if path else []), report.status.value

    def existing_report(self, status: RunStatus) -> tuple[list[str], str | None]:
        """Run encerrada por outro processo: reaproveita o relatório já gerado, se houver."""
        snapshots = self.history.list_report_snapshots(self.run_id)
        artifacts = {item.id: item for item in self.history.list_artifacts(self.run_id)}
        for snapshot in reversed(snapshots):
            artifact = artifacts.get(
                snapshot.report_artifact_id or snapshot.fallback_artifact_id or ""
            )
            if artifact is not None:
                return [str(self.history.run_dir(self.run_id) / artifact.relative_path)], (
                    snapshot.status.value
                )
        if status in (RunStatus.CANCELLED, RunStatus.LIMIT_REACHED, RunStatus.INTERRUPTED):
            paths, report_status = self._partial_after_external_end()
            return paths, report_status
        return [], None

    def _partial_after_external_end(self) -> tuple[list[str], str | None]:
        from lastro.reporting.builder import ReportBuilder

        record = self.history.get_run(self.run_id)
        try:
            report = ReportBuilder(self.history).build_partial(
                self.run_id, record.status_reason or record.status.value
            )
        except Exception:
            return [], "failed"
        path = report.report_path or report.fallback_path
        return ([str(path)] if path else []), report.status.value

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.close()
        if self.errlog is not None:
            with contextlib.suppress(OSError):
                self.errlog.close()
            self.errlog = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def exit_code(outcome: NativeOutcome) -> int:
    return 0 if outcome.status == RunStatus.COMPLETED.value else 1
