import asyncio
from pathlib import Path
from typing import Any

import pytest

from lastro.browser.video import (
    TENANT_REVERIFICATION_FAILED,
    VIDEO_ALREADY_ACTIVE,
    VIDEO_NOT_ACTIVE,
    EvidenceVideoRecorder,
    VideoError,
)
from lastro.history import (
    ArtifactType,
    CriterionOutcome,
    ExpectedSource,
    FindingClassification,
    ReportingError,
)
from lastro.reporting.models import parse_criterion_result, parse_finding


def finding(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "title": "Telefone não persiste após recarregar",
        "flow": "Cadastro de funcionário",
        "preconditions": ["Usuário autenticado no tenant esperado"],
        "steps": ["Abrir novo funcionário", "Preencher telefone", "Salvar", "Recarregar"],
        "expected": {
            "text": "O telefone salvo permanece visível",
            "source": "mission_criterion",
            "source_ref": "criterion_ab12",
        },
        "observed": "O campo aparece vazio após o reload.",
        "evidence": [{"artifact_id": "art_ab12", "purpose": "state"}],
        "classification": "suspected",
        "limitations": [],
        "failure_domain": "system",
        "severity_suggestion": "medium",
    }
    data.update(overrides)
    return data


def test_finding_input_reports_sorted_missing_fields() -> None:
    raw = finding(expected={"source": "heuristic"})
    for name in ("title", "steps", "failure_domain"):
        raw.pop(name)
    raw["observed"] = ""

    with pytest.raises(ReportingError) as info:
        parse_finding(raw, "action")

    assert info.value.code == "missing_finding_fields"
    assert info.value.detail == (
        "Missing finding fields: expected.text, failure_domain, observed, steps, title."
    )


def test_finding_input_maps_to_draft_without_provenance() -> None:
    draft = parse_finding(finding(), "action-1")

    assert draft.expected_source == ExpectedSource.MISSION_CRITERION
    assert draft.expected_source_ref == "criterion_ab12"
    assert draft.classification == FindingClassification.SUSPECTED
    assert [item.artifact_id for item in draft.evidence] == ["art_ab12"]
    assert draft.action_id == "action-1"
    with pytest.raises(ReportingError) as info:
        parse_finding(finding(tenant="outro"), None)
    assert info.value.code == "invalid_finding"


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "x" * 121},
        {"flow": "x" * 201},
        {"steps": ["passo"] * 31},
        {"preconditions": ["p"] * 21},
        {"observed": "x" * 4001},
        {"evidence": [{"artifact_id": f"art_{i}"} for i in range(21)]},
        {"expected": {"text": "esperado", "source": "mission_criterion"}},
        {"classification": "probable"},
        {"failure_domain": "network"},
    ],
)
def test_finding_input_enforces_limits_and_enums(overrides: dict[str, Any]) -> None:
    with pytest.raises(ReportingError) as info:
        parse_finding(finding(**overrides), None)
    assert info.value.code == "invalid_finding"


def test_criterion_result_input() -> None:
    draft = parse_criterion_result(
        {
            "criterion_id": "criterion_ab12",
            "outcome": "met",
            "evidence_artifact_ids": ["art_cd34"],
            "note": " O valor persistiu após recarregar. ",
            "reason": "positive_behavior_observed",
        }
    )
    assert (draft.outcome, draft.note) == (
        CriterionOutcome.MET,
        "O valor persistiu após recarregar.",
    )
    with pytest.raises(ReportingError):
        parse_criterion_result({"criterion_id": "c", "outcome": "passed", "reason": "r"})


class FakeHost:
    def __init__(self, tmp_path: Path, verified: list[bool]) -> None:
        self.lock = asyncio.Lock()
        self.tmp_path = tmp_path
        self.verified = verified
        self.recording: Path | None = None
        self.rotations: list[Path | None] = []

    async def rotate_context(self, *, record_video_dir: Path | None) -> Path | None:
        self.rotations.append(record_video_dir)
        previous, self.recording = self.recording, record_video_dir
        if previous is None:
            return None
        video = previous / "capture.webm"
        video.write_bytes(b"webm")
        return video

    async def verify(self, *, recovery: bool = False) -> bool:
        return self.verified.pop(0) if self.verified else True


class FakeHistory:
    def __init__(self) -> None:
        self.registered: list[tuple[str, ArtifactType, bytes]] = []

    def register_artifact(
        self, run_id: str, relative_path: str, artifact_type: ArtifactType, *, source: Path
    ) -> Any:
        self.registered.append((relative_path, artifact_type, source.read_bytes()))
        return type("Artifact", (), {"id": "art_video", "sha256": "0" * 64})()


def recorder(tmp_path: Path, host: FakeHost, history: FakeHistory, **kwargs: Any) -> Any:
    return EvidenceVideoRecorder(host, history, "run", tmp_path, **kwargs)  # type: ignore[arg-type]


def test_video_timeout_restores_verified_session(tmp_path: Path) -> None:
    host, history = FakeHost(tmp_path, []), FakeHistory()
    released = asyncio.Event()

    async def sleep(seconds: float) -> None:
        assert seconds == 60
        await released.wait()

    async def scenario() -> None:
        video = recorder(tmp_path, host, history, sleep=sleep, clock=lambda: 10.0)
        started = await video.start("timing of the save toast")
        assert started["max_duration_seconds"] == 60
        with pytest.raises(VideoError) as info:
            await video.start("again")
        assert info.value.code == VIDEO_ALREADY_ACTIVE
        released.set()
        assert video.timer is not None
        await video.timer
        result = await video.stop(started["recording_id"])
        assert result["stop_reason"] == "timeout"
        assert result["artifact_id"] == "art_video"
        with pytest.raises(VideoError) as info:
            await video.stop("video_unknown")
        assert info.value.code == VIDEO_NOT_ACTIVE

    asyncio.run(scenario())
    assert history.registered[0][1] == ArtifactType.VIDEO
    assert history.registered[0][0].startswith("evidence/video_")
    assert history.registered[0][0].endswith(".webm")
    assert host.rotations[-1] is None
    assert host.recording is None
    assert not any((tmp_path / ".video").iterdir())


def test_video_failed_reverification_discards_capture(tmp_path: Path) -> None:
    host, history = FakeHost(tmp_path, [True, False]), FakeHistory()

    async def scenario() -> None:
        video = recorder(tmp_path, host, history, sleep=lambda _: asyncio.Event().wait())
        started = await video.start("timing")
        with pytest.raises(VideoError) as info:
            await video.stop(started["recording_id"])
        assert info.value.code == TENANT_REVERIFICATION_FAILED
        assert video.active is None

    asyncio.run(scenario())
    assert history.registered == []
    assert host.recording is None


def test_video_start_fails_closed_when_tenant_changes(tmp_path: Path) -> None:
    host, history = FakeHost(tmp_path, [False]), FakeHistory()

    async def scenario() -> None:
        video = recorder(tmp_path, host, history)
        with pytest.raises(VideoError) as info:
            await video.start("timing")
        assert info.value.code == TENANT_REVERIFICATION_FAILED
        assert video.active is None

    asyncio.run(scenario())
    assert host.recording is None
    assert history.registered == []
