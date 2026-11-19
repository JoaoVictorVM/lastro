from collections.abc import Callable
from pathlib import Path

from lastro.cli.output import Status
from lastro.doctor.checks import DoctorEnvironment, check_document_sources
from lastro.sources.embeddings import ModelStatus, model_status, prepare_model


def test_check_is_absent_without_the_extra(doctor_env: Callable[..., DoctorEnvironment]) -> None:
    assert check_document_sources(doctor_env(), Path("root")) is None


def test_check_reports_ready_and_missing_model(
    doctor_env: Callable[..., DoctorEnvironment],
) -> None:
    ready = ModelStatus(True, None, "f" * 64, "intfloat/multilingual-e5-large preparado")
    missing = ModelStatus(False, "model_not_ready", None, "modelo local não preparado")
    absent = ModelStatus(False, "sources_unavailable", None, "extra sources não instalado")

    ok = check_document_sources(doctor_env(sources_status=lambda root: ready), Path("r"))
    warn = check_document_sources(doctor_env(sources_status=lambda root: missing), Path("r"))
    extra = check_document_sources(doctor_env(sources_status=lambda root: absent), Path("r"))

    assert ok is not None and ok.status is Status.OK
    assert warn is not None and warn.status is Status.WARN
    assert "lastro sources model prepare" in (warn.fix or "")
    assert extra is not None and "--extra sources" in (extra.fix or "")


def test_prepare_writes_verifiable_manifest(tmp_path: Path) -> None:
    import pytest

    pytest.importorskip("fastembed")
    pytest.importorskip("sqlite_vec")

    def fake_download(cache: Path) -> None:
        snapshot = cache / "models--x--e5" / "snapshots" / "rev1"
        snapshot.mkdir(parents=True)
        (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
        (snapshot / "model.onnx").write_bytes(b"weights")

    assert not model_status(tmp_path).ready
    fingerprint = prepare_model(tmp_path, fake_download)
    status = model_status(tmp_path)

    assert status.ready and status.fingerprint == fingerprint
    weights = next(tmp_path.rglob("model.onnx"))
    weights.write_bytes(b"tampered weights")
    assert model_status(tmp_path).reason == "model_not_ready"
