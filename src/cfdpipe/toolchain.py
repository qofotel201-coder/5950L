"""Resolve external CFD tool executables without silently changing sources.

Resolution is intentionally policy-heavy: an explicitly configured candidate is
either accepted or reported as invalid.  It is never replaced by a lower-priority
candidate, which keeps command provenance reproducible.
"""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence


TOOL_NAMES = ("gmsh", "su2_cfd", "su2_sol", "mpiexec", "pvbatch")

ENV_VARS = {
    "gmsh": "CFD_GMSH",
    "su2_cfd": "CFD_SU2_CFD",
    "su2_sol": "CFD_SU2_SOL",
    "mpiexec": "CFD_MPIEXEC",
    "pvbatch": "CFD_PVBATCH",
}

EXECUTABLE_NAMES = {
    "gmsh": "gmsh",
    "su2_cfd": "SU2_CFD",
    "su2_sol": "SU2_SOL",
    "mpiexec": "mpiexec",
    "pvbatch": "pvbatch",
}

_WINDOWS_DIRECTORY_RE = re.compile(
    r"^[A-Za-z]:[\\/]Windows(?:[\\/]|$)", re.IGNORECASE
)
_MNT_C_EXE_RE = re.compile(r"^/mnt/c(?:/|$).*\.exe$", re.IGNORECASE)
_UNSET = object()


class ToolchainError(RuntimeError):
    """Base class for toolchain configuration and resolution failures."""


class ToolchainConfigError(ToolchainError):
    """Raised when ``config/tools.json`` is malformed."""


class ToolResolutionError(ToolchainError):
    """Raised when a requested executable cannot be safely resolved."""


@dataclass(frozen=True)
class ResolvedTool:
    """A validated executable and the source that selected it."""

    name: str
    path: Path
    source: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""

        return {"name": self.name, "path": str(self.path), "source": self.source}


class Toolchain:
    """Load tool configuration and resolve executable paths.

    The strict resolution order is CLI override, environment variable,
    ``config/tools.json``, then ``shutil.which``.  A present but invalid value
    stops resolution instead of silently falling through.
    """

    def __init__(
        self,
        config_path: str | os.PathLike[str] = "config/tools.json",
        *,
        cli_overrides: Mapping[str, str | os.PathLike[str] | None] | None = None,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve(strict=False)
        self._cli_overrides = dict(cli_overrides or {})
        self._environ = dict(os.environ if environ is None else environ)
        self._which = which
        self._config_tools, self.allow_mnt_c_executables = self._read_config()

    @classmethod
    def load(
        cls,
        config_path: str | os.PathLike[str] = "config/tools.json",
        **kwargs: object,
    ) -> "Toolchain":
        """Construct a toolchain from a JSON configuration file."""

        return cls(config_path=config_path, **kwargs)

    def resolve(
        self,
        name: str,
        *,
        cli_override: str | os.PathLike[str] | None | object = _UNSET,
    ) -> ResolvedTool:
        """Resolve and validate one supported tool.

        ``None`` means that a CLI option was not supplied.  An empty string is a
        supplied, invalid value and therefore prevents fallback.
        """

        resolved, env_name, search_names = self._resolve_candidate(
            name, cli_override=cli_override
        )
        if resolved is None:
            raise ToolResolutionError(
                f"Unable to resolve tool '{name}': no CLI override, "
                f"{env_name} environment value, or configured path was supplied, "
                f"and shutil.which candidates {search_names!r} found nothing."
            )
        return resolved

    def resolve_optional(
        self,
        name: str,
        *,
        cli_override: str | os.PathLike[str] | None | object = _UNSET,
    ) -> ResolvedTool | None:
        """Resolve an optional tool without hiding an explicitly invalid path."""

        resolved, _env_name, _search_names = self._resolve_candidate(
            name, cli_override=cli_override
        )
        return resolved

    def _resolve_candidate(
        self,
        name: str,
        *,
        cli_override: str | os.PathLike[str] | None | object,
    ) -> tuple[ResolvedTool | None, str, tuple[str, ...]]:
        self._require_known_tool(name)

        env_name = ENV_VARS[name]
        search_names = (
            ("mpirun", "mpiexec")
            if name == "mpiexec"
            else (EXECUTABLE_NAMES[name],)
        )
        if cli_override is not _UNSET and cli_override is not None:
            return self._resolve_selected(name, cli_override, "cli"), env_name, search_names
        if name in self._cli_overrides and self._cli_overrides[name] is not None:
            return (
                self._resolve_selected(name, self._cli_overrides[name], "cli"),
                env_name,
                search_names,
            )
        if env_name in self._environ:
            return (
                self._resolve_selected(name, self._environ[env_name], "env"),
                env_name,
                search_names,
            )
        configured = self._config_tools.get(name)
        if configured is not None:
            return self._resolve_selected(name, configured, "config"), env_name, search_names

        discovered = None
        for executable_name in search_names:
            if self._which is None:
                discovered = shutil.which(executable_name, path=self._search_path())
            else:
                # Keep the one-argument injection interface used by tests/callers.
                discovered = self._which(executable_name)
            if discovered is not None:
                break
        if discovered is None:
            return None, env_name, search_names
        return self._resolve_selected(name, discovered, "which"), env_name, search_names

    def check(
        self,
        names: Sequence[str] | None = None,
        *,
        cli_overrides: Mapping[str, str | os.PathLike[str] | None] | None = None,
    ) -> dict[str, ResolvedTool]:
        """Resolve a sequence of tools, stopping immediately on the first error."""

        requested = TOOL_NAMES if names is None else tuple(names)
        one_shot = dict(cli_overrides or {})
        results: dict[str, ResolvedTool] = {}
        for name in requested:
            if name in one_shot:
                results[name] = self.resolve(name, cli_override=one_shot[name])
            else:
                results[name] = self.resolve(name)
        return results

    def _search_path(self) -> str:
        """Return only the PATH supplied through the injected environment.

        Passing an empty string to ``shutil.which`` is deliberate: passing
        ``None`` would make it consult the host process environment.
        """

        if "PATH" in self._environ:
            return self._environ["PATH"]
        if os.name == "nt":
            for key, value in self._environ.items():
                if key.upper() == "PATH":
                    return value
        return ""

    def _read_config(self) -> tuple[dict[str, str | None], bool]:
        if not self.config_path.exists():
            return {}, False
        if not self.config_path.is_file():
            raise ToolchainConfigError(
                f"Tool configuration path is not a file: {self.config_path}"
            )

        try:
            with self.config_path.open("r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolchainConfigError(
                f"Unable to read tool configuration {self.config_path}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ToolchainConfigError(
                f"Tool configuration root must be an object: {self.config_path}"
            )

        allow_mnt = data.get("allow_mnt_c_executables", False)
        if not isinstance(allow_mnt, bool):
            raise ToolchainConfigError(
                "'allow_mnt_c_executables' must be a JSON boolean."
            )

        tools = data.get("tools", {})
        if not isinstance(tools, dict):
            raise ToolchainConfigError("'tools' must be a JSON object.")

        normalized: dict[str, str | None] = {}
        for name in TOOL_NAMES:
            value = tools.get(name)
            if value is not None and not isinstance(value, str):
                raise ToolchainConfigError(
                    f"Configured path for tool '{name}' must be a string or null."
                )
            normalized[name] = value
        return normalized, allow_mnt

    @staticmethod
    def _require_known_tool(name: str) -> None:
        if name not in TOOL_NAMES:
            supported = ", ".join(TOOL_NAMES)
            raise ToolResolutionError(
                f"Unknown tool '{name}'. Supported tools: {supported}."
            )

    def _resolve_selected(
        self,
        name: str,
        candidate: object,
        source: str,
    ) -> ResolvedTool:
        try:
            path = self._validate_candidate(name, candidate, source)
        except ToolResolutionError as exc:
            if source == "which":
                raise
            raise ToolResolutionError(
                f"Invalid {source} candidate for tool '{name}': {exc} "
                "Lower-priority sources were not considered."
            ) from exc
        return ResolvedTool(name=name, path=path, source=source)

    def _validate_candidate(
        self, name: str, candidate: object, source: str
    ) -> Path:
        try:
            raw_path = os.fspath(candidate)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ToolResolutionError(
                f"executable path must be a string or path-like value, got "
                f"{type(candidate).__name__}."
            ) from exc

        if isinstance(raw_path, bytes):
            raw_path = os.fsdecode(raw_path)
        if not raw_path or raw_path.isspace():
            raise ToolResolutionError("executable path is empty.")

        canonical_mnt_c = _canonical_mnt_c_executable(raw_path)
        if canonical_mnt_c is not None and not self.allow_mnt_c_executables:
            raise ToolResolutionError(
                f"/mnt/c Windows executable is disallowed: {raw_path!r}. "
                "Only allow_mnt_c_executables=true in config/tools.json may "
                "enable it."
            )

        if source == "which" and _is_windows_directory(raw_path):
            raise ToolResolutionError(
                "shutil.which returned a silent Windows executable from a "
                f"Windows system directory: {raw_path!r}."
            )

        path = _path_from_candidate(raw_path, canonical_mnt_c)
        path = path.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve(strict=False)

        final_mnt_c = _canonical_mnt_c_executable(str(path))
        if final_mnt_c is not None and not self.allow_mnt_c_executables:
            raise ToolResolutionError(
                f"/mnt/c Windows executable is disallowed after path "
                f"normalization: {path}. Only allow_mnt_c_executables=true "
                "in config/tools.json may enable it."
            )
        if source == "which" and _is_windows_directory(str(path)):
            raise ToolResolutionError(
                "shutil.which returned a silent Windows executable from a "
                f"Windows system directory: {path}."
            )
        if name == "pvbatch" and path.stem.lower() != "pvbatch":
            raise ToolResolutionError(
                "pvbatch must resolve to pvbatch/pvbatch.exe; ParaView GUI or "
                f"other executables are forbidden: {path}"
            )
        if os.name != "nt" and path.name.lower().endswith(".exe"):
            raise ToolResolutionError(
                f"Windows .exe executable is disallowed on POSIX: {path}."
            )
        if not path.is_file():
            raise ToolResolutionError(
                f"executable path does not exist or is not a file: {path}"
            )
        if not os.access(path, os.X_OK):
            raise ToolResolutionError(f"executable path is not executable: {path}")
        if not path.is_absolute():  # Defensive invariant for mocked path objects.
            raise ToolResolutionError(f"resolved path is not absolute: {path}")
        return path


def _is_windows_directory(path: str) -> bool:
    """Recognize a drive-rooted Windows directory on any host platform."""

    normalized = ntpath.normpath(path)
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return bool(_WINDOWS_DIRECTORY_RE.match(normalized))


def _canonical_mnt_c_executable(path: str) -> str | None:
    """Return a canonical WSL C-drive executable path, if one was supplied.

    This lexical normalization closes variants such as ``//mnt/c`` and
    ``/mnt/../mnt/c``.  Calling it again after ``Path.resolve`` also covers
    symbolic links whose final target is a mounted Windows executable.
    """

    slash_path = path.replace("\\", "/")
    if not slash_path.startswith("/"):
        return None
    slash_path = "/" + slash_path.lstrip("/")
    canonical = posixpath.normpath(slash_path)
    if _MNT_C_EXE_RE.match(canonical):
        return canonical
    return None


def _path_from_candidate(
    raw_path: str, canonical_mnt_c: str | None
) -> Path:
    """Translate an explicitly allowed WSL C-drive path for Windows Python."""

    if canonical_mnt_c is not None and os.name == "nt":
        parts = PurePosixPath(canonical_mnt_c).parts
        # parts begins with ('/', 'mnt', 'c'); canonicalization guarantees this.
        return Path("C:/", *parts[3:])
    return Path(raw_path)


__all__ = [
    "ENV_VARS",
    "EXECUTABLE_NAMES",
    "ResolvedTool",
    "TOOL_NAMES",
    "ToolResolutionError",
    "Toolchain",
    "ToolchainConfigError",
    "ToolchainError",
]
