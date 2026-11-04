"""Leitura de revisões, metadados e objetos Git sem alterar branch, índice ou working tree.

Só comandos de leitura são usados (rev-parse, ls-tree, ls-files, cat-file, merge-base, config);
nunca `git diff`, `git status` com inspeção de conteúdo, checkout, stash ou reset. `fetch` só
roda com consentimento explícito e toca apenas refs, objetos e FETCH_HEAD.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from lastro.config.source import is_valid_ref

GIT_TIMEOUT_SECONDS = 10.0
FETCH_TIMEOUT_SECONDS = 60.0
_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SCP_REMOTE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class GitError(Exception):
    """Falha de um comando Git; a mensagem nunca carrega stdout/stderr bruto."""


class GitUnavailable(GitError):
    """O repositório não pode ser inspecionado sem relaxar a proteção."""


def is_oid(value: str) -> bool:
    return _OID.match(value) is not None


@dataclass(frozen=True)
class Entry:
    path: str
    mode: str
    oid: str
    size: int | None


def _sanitized_env() -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_") and "GITLEAKS" not in key.upper()
    }
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "LC_ALL": "C",
        }
    )
    return env


def normalize_remote(url: str) -> str | None:
    """Identidade `host/owner/repo` sem credenciais; None para transportes não suportados."""
    if url.startswith("https://"):
        rest = url[len("https://") :]
        host, _, path = rest.partition("/")
        host = host.rpartition("@")[2].split(":")[0]
    elif url.startswith("ssh://"):
        rest = url[len("ssh://") :]
        host, _, path = rest.partition("/")
        host = host.rpartition("@")[2].split(":")[0]
    elif _SCP_REMOTE.match(url):
        host, _, path = url.partition("@")[2].partition(":")
    else:
        return None
    path = path.strip("/").removesuffix(".git")
    if not host or not path or ".." in path.split("/"):
        return None
    return f"{host.lower()}/{path}"


@dataclass
class Git:
    """Executável resolvido pelo processo confiável, lista de argumentos e shell desabilitado."""

    root: Path
    executable: str | None = None
    timeout: float = GIT_TIMEOUT_SECONDS
    allowed_protocols: tuple[str, ...] = ("https", "ssh")
    blob_requests: list[str] = field(default_factory=list[str])
    _hooks_dir: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.executable = self.executable or shutil.which("git")
        if self.executable is None:
            raise GitUnavailable("git not found")
        # Diretório vazio para hooks: nenhum hook indicado pelo repositório é executado.
        self._hooks_dir = tempfile.mkdtemp(prefix="lastro-nohooks-")

    def close(self) -> None:
        shutil.rmtree(self._hooks_dir, ignore_errors=True)

    def _config(self) -> list[str]:
        settings = [
            "core.fsmonitor=false",
            f"core.hooksPath={self._hooks_dir}",
            "core.pager=cat",
            "core.untrackedCache=false",
            "core.sshCommand=ssh",
            "diff.external=",
            "submodule.recurse=false",
            "fetch.recurseSubmodules=false",
            "protocol.allow=never",
            *(f"protocol.{name}.allow=always" for name in self.allowed_protocols),
        ]
        return [item for setting in settings for item in ("-c", setting)]

    def run(
        self,
        args: Sequence[str],
        *,
        input_data: bytes | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> bytes:
        assert self.executable is not None
        command = [self.executable, "--no-pager", "-C", str(self.root), *self._config(), *args]
        try:
            completed = subprocess.run(
                command,
                input=input_data,
                capture_output=True,
                timeout=timeout or self.timeout,
                env=_sanitized_env(),
                stdin=None if input_data is not None else subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out") from None
        except OSError:
            raise GitUnavailable("git could not be started") from None
        if check and completed.returncode != 0:
            raise GitError(f"git {args[0]} failed (exit {completed.returncode})")
        return completed.stdout if completed.returncode == 0 else b""

    def text(self, *args: str, check: bool = True) -> str:
        return self.run(args, check=check).decode("utf-8", "replace").strip()

    def is_top_level(self) -> bool:
        try:
            inside = self.text("rev-parse", "--is-inside-work-tree")
            top = self.text("rev-parse", "--show-toplevel")
        except GitError:
            return False
        if inside != "true" or not top:
            return False
        return os.path.normcase(os.path.realpath(top)) == os.path.normcase(
            os.path.realpath(self.root)
        )

    def resolve(self, ref: str) -> str | None:
        """OID completo de uma ref validada ou de um OID; sintaxe de revisão é recusada."""
        if not (is_oid(ref) or ref == "HEAD" or is_valid_ref(ref)):
            raise GitError("invalid ref")
        oid = self.text(
            "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False
        )
        return oid if is_oid(oid) else None

    def has_commit(self, oid: str) -> bool:
        if not is_oid(oid):
            return False
        return self.resolve(oid) == oid

    def branch(self) -> str | None:
        name = self.text("symbolic-ref", "-q", "--short", "HEAD", check=False)
        return name or None

    def merge_base(self, first: str, second: str) -> str | None:
        if not (is_oid(first) and is_oid(second)):
            return None
        oid = self.text("merge-base", "--end-of-options", first, second, check=False)
        return oid if is_oid(oid) else None

    def ls_tree(self, commit: str) -> dict[str, Entry]:
        """Caminhos delimitados por NUL com modo, OID e tamanho, sem ler conteúdo."""
        if not is_oid(commit):
            raise GitError("invalid commit")
        raw = self.run(["ls-tree", "-r", "-z", "-l", "--full-tree", "--end-of-options", commit])
        entries: dict[str, Entry] = {}
        for record in raw.split(b"\x00"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, kind, oid, size = meta.decode("ascii").split(maxsplit=3)
            name = path.decode("utf-8", "surrogateescape")
            entries[name] = Entry(name, mode, oid, int(size) if kind == "blob" else None)
        return entries

    def ls_index(self) -> tuple[dict[str, Entry], set[str]]:
        """Índice por metadados; caminhos em conflito (stage > 0) voltam à parte."""
        raw = self.run(["ls-files", "-s", "-z"])
        entries: dict[str, Entry] = {}
        conflicted: set[str] = set()
        for record in raw.split(b"\x00"):
            if not record:
                continue
            meta, _, path = record.partition(b"\t")
            mode, oid, stage = meta.decode("ascii").split()
            name = path.decode("utf-8", "surrogateescape")
            if stage != "0":
                conflicted.add(name)
                continue
            entries[name] = Entry(name, mode, oid, None)
        sizes = self.sizes([entry.oid for entry in entries.values() if entry.mode != "160000"])
        return {
            name: Entry(name, entry.mode, entry.oid, sizes.get(entry.oid))
            for name, entry in entries.items()
        }, conflicted

    def sizes(self, oids: Sequence[str]) -> dict[str, int]:
        if not oids:
            return {}
        raw = self.run(
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            input_data=("\n".join(oids) + "\n").encode("ascii"),
        )
        found: dict[str, int] = {}
        for line in raw.decode("ascii", "replace").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == "blob":
                found[parts[0]] = int(parts[2])
        return found

    def untracked(self) -> list[str]:
        """Somente nomes não ignorados; nenhum conteúdo é lido aqui."""
        raw = self.run(["ls-files", "-z", "--others", "--exclude-standard"])
        return [item.decode("utf-8", "surrogateescape") for item in raw.split(b"\x00") if item]

    def blob(self, oid: str, size: int) -> bytes:
        """Lê um blob já aprovado por caminho e tamanho; sem filtros nem textconv."""
        if not is_oid(oid):
            raise GitError("invalid blob")
        self.blob_requests.append(oid)
        data = self.run(["cat-file", "blob", oid])
        if len(data) != size:
            raise GitError("blob size mismatch")
        return data

    def index_identity(self) -> tuple[int, int] | None:
        relative = self.text("rev-parse", "--git-path", "index", check=False)
        if not relative:
            return None
        path = Path(relative) if Path(relative).is_absolute() else self.root / relative
        try:
            info = path.stat()
        except OSError:
            return None
        return info.st_size, info.st_mtime_ns

    def remotes(self) -> dict[str, str]:
        raw = self.text("config", "--get-regexp", r"^remote\..*\.url$", check=False)
        found: dict[str, str] = {}
        for line in raw.splitlines():
            key, _, url = line.partition(" ")
            name = key.removeprefix("remote.").removesuffix(".url")
            if _REMOTE_NAME.match(name):
                found[name] = url.strip()
        return found

    def fetch(self, remote: str) -> None:
        """Atualiza só refs/objetos/FETCH_HEAD de um remote já configurado e validado."""
        url = self.remotes().get(remote)
        if url is None or not _REMOTE_NAME.match(remote):
            raise GitError("remote not configured")
        if normalize_remote(url) is None and "file" not in self.allowed_protocols:
            raise GitError("remote transport not allowed")
        self.run(
            [
                "fetch",
                "--no-tags",
                "--no-recurse-submodules",
                "--quiet",
                "--end-of-options",
                remote,
            ],
            timeout=FETCH_TIMEOUT_SECONDS,
        )


def git_blob_oid(data: bytes, length: int = 40) -> str:
    """OID que o Git daria ao conteúdo, para comparar working tree e índice sem `git status`."""
    import hashlib

    algorithm = hashlib.sha1 if length == 40 else hashlib.sha256
    return algorithm(b"blob %d\x00" % len(data) + data).hexdigest()
