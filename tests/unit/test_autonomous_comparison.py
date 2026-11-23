from lastro.autonomous.comparison import (
    COMPLETE,
    NONE,
    Candidate,
    Comparable,
    match,
    normalize,
)
from lastro.autonomous.models import MatchLabel

STEPS = ("Abrir /employees/new", "Preencher o CPF 123.456.789-00", "Clicar em #save")


def finding(
    finding_id: str,
    *,
    flow: str = "employee.create",
    title: str = "CPF inválido é aceito",
    steps: tuple[str, ...] = STEPS,
    observed: str = "O cadastro foi salvo com CPF inválido",
) -> Comparable:
    return Comparable(finding_id, flow, title, steps, observed)


COVERED = {"employeecreate": COMPLETE, "employee.create": COMPLETE}


def _coverage(*flows: str, state: str = COMPLETE) -> dict[str, str]:
    return {normalize(flow): state for flow in flows}


def _labels(records: tuple[object, ...]) -> list[tuple[str, str | None, str | None]]:
    return [
        (r.label.value, r.current_finding_id, r.previous_finding_id)  # type: ignore[attr-defined]
        for r in records
    ]


def test_normalization_keeps_numbers_selectors_and_order() -> None:
    assert normalize("  Clicar   em #save, depois.  ") == "clicar em #save depois"
    assert normalize("CPF 123.456.789-00") == "cpf 12345678900"
    assert normalize("ＡＢＣ [name='x']") == "abc [name='x']"  # noqa: RUF001
    assert normalize("b a") != normalize("a b")


def test_exact_signature_wins() -> None:
    current = finding("cur", title="Outro título", observed="Outra observação")
    previous = finding("prev", title="Título original")
    records = match(
        [current], [Candidate(previous)], _coverage("employee.create"), current_complete=True
    )
    (record,) = records
    assert record.label == MatchLabel.RECURRING
    assert record.match_rule == "exact_signature" and record.score == 1.0


def test_weighted_jaccard_threshold_and_margin() -> None:
    near = finding("cur", steps=(*STEPS[:2], "Clicar em #save agora"))
    far = finding(
        "cur2",
        steps=("Abrir /reports", "Exportar CSV"),
        title="Relatório vazio",
        observed="Arquivo sem linhas",
    )
    previous = finding("prev")
    records = match(
        [near, far], [Candidate(previous)], _coverage("employee.create"), current_complete=True
    )
    by_current = {r.current_finding_id: r for r in records if r.current_finding_id}
    assert by_current["cur"].label == MatchLabel.RECURRING
    assert by_current["cur"].match_rule == "weighted_jaccard"
    assert by_current["cur"].score is not None and by_current["cur"].score >= 0.80
    assert by_current["cur2"].label == MatchLabel.NEW
    assert by_current["cur2"].match_rule == "below_threshold"
    other_flow = finding("cur3", flow="employee.edit")
    (record,) = [
        r
        for r in match([other_flow], [Candidate(finding("p"))], {}, current_complete=True)
        if r.current_finding_id
    ]
    assert record.label == MatchLabel.NEW and record.match_rule == "no_candidate"


def test_ambiguous_match_is_new() -> None:
    current = finding("cur")
    twins = [Candidate(finding("a")), Candidate(finding("b"))]
    records = match([current], twins, _coverage("employee.create"), current_complete=True)
    current_records = [r for r in records if r.current_finding_id == "cur"]
    assert current_records[0].label == MatchLabel.NEW
    assert current_records[0].match_rule == "ambiguous"
    close = (
        Candidate(finding("a", steps=(*STEPS[:2], "Clicar em #save rapidamente"))),
        Candidate(finding("b", steps=(*STEPS[:2], "Clicar em #save devagar"))),
    )
    near = finding("cur", steps=(*STEPS[:2], "Clicar em #save"))
    (record,) = [
        r
        for r in match([near], close, _coverage("employee.create"), current_complete=True)
        if r.current_finding_id
    ]
    assert record.label == MatchLabel.NEW


def test_regression_after_resolved() -> None:
    current = finding("cur")
    resolved = Candidate(finding("old"), resolved_before=True)
    (record,) = match([current], [resolved], _coverage("employee.create"), current_complete=True)
    assert record.label == MatchLabel.REGRESSED
    assert record.previous_finding_id == "old"
    resolved_now = match([], [Candidate(finding("p"))], COVERED, current_complete=True)
    assert [r.label for r in resolved_now] == [MatchLabel.RESOLVED]
    silent = match([], [resolved], COVERED, current_complete=True)
    assert silent == ()


def test_uncovered_flow_is_not_resolved() -> None:
    previous = [Candidate(finding("p"))]
    uncovered = match([], previous, _coverage("employee.create", state=NONE), current_complete=True)
    assert [r.label for r in uncovered] == [MatchLabel.NOT_COMPARABLE]
    blocked = match([], previous, COVERED, current_complete=False)
    assert [r.label for r in blocked] == [MatchLabel.NOT_COMPARABLE]
    assert blocked[0].match_rule == "flow_not_covered"


def test_matching_is_stable() -> None:
    currents = [finding(f"c{index}", title=f"Título {index}") for index in range(3)]
    previous = [Candidate(finding("p1")), Candidate(finding("p0", title="Título 0"))]
    first = match(currents, previous, COVERED, current_complete=True)
    second = match(
        list(reversed(currents)), list(reversed(previous)), COVERED, current_complete=True
    )
    assert first == second
    paired = [r for r in first if r.previous_finding_id and r.current_finding_id]
    assert len({r.previous_finding_id for r in paired}) == len(paired)
    assert len({r.current_finding_id for r in first if r.current_finding_id}) == 3
