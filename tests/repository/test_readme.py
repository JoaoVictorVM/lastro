import re
import shlex
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lastro.cli.app import app

REPO_ROOT = Path(__file__).resolve().parents[2]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
GIF_LINK = re.compile(r"!\[[^\]]*\]\((docs/assets/[^)]+\.gif)\)")
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
MAX_GIF_BYTES = 8 * 1024 * 1024
MAX_GIF_SECONDS = 60
QUICKSTART_TOOLS = {"git", "cd", "uv", "lastro"}
REQUIRED_SECTIONS = (
    "## What Lastro guarantees / does not guarantee",
    "## Quickstart",
    "## Architecture",
    "## Evaluation results",
    "## Supported executors",
    "## Roadmap",
    "## License",
)


def section(title: str) -> str:
    start = README.index(f"\n{title}\n")
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def code_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"```{language}\n(.*?)```", text, flags=re.DOTALL)


def slug(heading: str) -> str:
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def anchors(markdown: str) -> set[str]:
    return {slug(match) for match in re.findall(r"^#+ (.+)$", markdown, flags=re.MULTILINE)}


def test_readme_sections_follow_required_order() -> None:
    lines = README.splitlines()
    assert lines[0] == "# Lastro"
    description = next(line for line in lines[1:] if line.strip())
    assert description.endswith(".")
    assert description.count(". ") == 0, "the description must be a single sentence"
    gif = GIF_LINK.search(README)
    assert gif is not None

    positions = [README.index(description), gif.start()]
    positions += [README.index(f"\n{title}\n") for title in REQUIRED_SECTIONS]

    assert positions == sorted(positions)


def quickstart_commands() -> list[str]:
    commands: list[str] = []
    for block in code_blocks(section("## Quickstart"), "bash"):
        commands += [
            line.strip() for line in block.splitlines() if line.strip() and not line.startswith("#")
        ]
    return commands


def test_quickstart_has_at_most_six_commands() -> None:
    commands = quickstart_commands()

    assert 0 < len(commands) <= 6
    assert {shlex.split(command)[0] for command in commands} <= QUICKSTART_TOOLS
    assert "uv tool install ." in commands
    assert "lastro demo up" in commands


@pytest.mark.parametrize("command", [c for c in quickstart_commands() if c.startswith("lastro ")])
def test_quickstart_lastro_commands_exist(command: str) -> None:
    result = CliRunner().invoke(app, [*shlex.split(command)[1:], "--help"])

    assert result.exit_code == 0, result.output


def test_quickstart_states_full_mission_is_not_available() -> None:
    text = section("## Quickstart")

    assert "not available yet" in text
    assert "(#roadmap)" in text


def gif_duration_seconds(data: bytes) -> float:
    """Sum of frame delays from the Graphic Control Extensions of a GIF89a stream."""
    assert data[:6] in (b"GIF87a", b"GIF89a")
    flags = data[10]
    offset = 13
    if flags & 0x80:
        offset += 3 * (2 ** ((flags & 0x07) + 1))
    total_centiseconds = 0
    while offset < len(data):
        marker = data[offset]
        if marker == 0x3B:
            break
        if marker == 0x21:
            label = data[offset + 1]
            offset += 2
            if label == 0xF9:
                total_centiseconds += int.from_bytes(data[offset + 2 : offset + 4], "little")
            offset = skip_sub_blocks(data, offset)
        elif marker == 0x2C:
            image_flags = data[offset + 9]
            offset += 10
            if image_flags & 0x80:
                offset += 3 * (2 ** ((image_flags & 0x07) + 1))
            offset = skip_sub_blocks(data, offset + 1)
        else:
            raise AssertionError(f"unexpected GIF block 0x{marker:02x} at {offset}")
    return total_centiseconds / 100


def skip_sub_blocks(data: bytes, offset: int) -> int:
    while data[offset] != 0:
        offset += data[offset] + 1
    return offset + 1


def test_demo_asset_within_limits() -> None:
    gif = GIF_LINK.search(README)
    assert gif is not None
    path = REPO_ROOT / gif.group(1)

    assert path.is_file()
    assert path.stat().st_size <= MAX_GIF_BYTES
    assert 0 < gif_duration_seconds(path.read_bytes()) <= MAX_GIF_SECONDS


def test_architecture_mermaid_present() -> None:
    blocks = code_blocks(section("## Architecture"), "mermaid")

    assert len(blocks) == 1
    assert "classDef implemented" in blocks[0]
    assert "classDef planned" in blocks[0]
    assert re.search(r"^\s*class .+ implemented$", blocks[0], flags=re.MULTILINE)
    assert re.search(r"^\s*class .+ planned$", blocks[0], flags=re.MULTILINE)


def test_evaluation_status_is_truthful() -> None:
    text = section("## Evaluation results")
    rows = [line for line in text.splitlines() if line.startswith("|")][2:]

    assert rows
    for row in rows:
        result = row.strip("|").split("|")[1].strip()
        assert result == "Not measured yet", row
    assert not re.search(r"\d", text)
    assert "No evaluation has been run yet" in text


def test_supported_executors_mark_planned_ones() -> None:
    text = section("## Supported executors")

    assert "Claude Code" in text
    assert "| Codex | Planned |" in text


@pytest.mark.parametrize(
    "document", ["README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"]
)
def test_required_links_resolve(document: str) -> None:
    markdown = (REPO_ROOT / document).read_text(encoding="utf-8")
    for target in LINK.findall(markdown):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path_part, _, fragment = target.partition("#")
        linked = (REPO_ROOT / path_part) if path_part else REPO_ROOT / document
        assert linked.exists(), f"{document}: broken link {target}"
        if fragment:
            assert fragment in anchors(linked.read_text(encoding="utf-8")), (
                f"{document}: missing anchor {target}"
            )
    if document == "README.md":
        for required in ("(#roadmap)", "(LICENSE)", "(SECURITY.md)"):
            assert required in markdown, required
