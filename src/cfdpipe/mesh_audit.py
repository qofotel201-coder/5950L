"""Bounded-memory textual audit for three-dimensional SU2 meshes.

The production coarse-mesh gate cannot reuse the smoke parser: that parser is
deliberately capped at 300,000 cells and retains complete connectivity for its
small-mesh topology checks.  This module performs the independent serialized
file checks that can be evaluated in constant auxiliary memory.  It never
loads the complete file or stores volume/marker connectivity.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


PathLike = str | os.PathLike[str]

_SCHEMA = "cfdpipe.su2_mesh_audit.v1"
_COUNT_KEYS = frozenset({"NDIME", "NELEM", "NPOIN", "NMARK"})
_NONFINITE_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])[+-]?(?:nan|inf(?:inity)?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_VOLUME_TYPES: Mapping[int, tuple[str, int]] = {
    10: ("Tetrahedron 4", 4),
    13: ("Prism 6", 6),
    14: ("Pyramid 5", 5),
}
_BOUNDARY_TYPES: Mapping[int, tuple[str, int]] = {
    5: ("Triangle 3", 3),
    9: ("Quadrilateral 4", 4),
}
_MAX_LINE_CHARACTERS = 1024 * 1024


class SU2MeshAuditError(RuntimeError):
    """Internal fail-closed parsing error retained in returned evidence."""

    def __init__(
        self,
        message: str,
        *,
        line_number: int | None = None,
        state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.line_number = line_number
        self.state = state


def _json_safe(value: object) -> None:
    """Raise if an audit result cannot be encoded as strict JSON."""

    json.dumps(value, allow_nan=False)


def _normalize_element_range(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
    ):
        raise SU2MeshAuditError("target_element_range must contain two integers")
    minimum, maximum = value
    if (
        isinstance(minimum, bool)
        or not isinstance(minimum, int)
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or minimum <= 0
        or maximum < minimum
    ):
        raise SU2MeshAuditError(
            "target_element_range must be a positive inclusive integer range"
        )
    return int(minimum), int(maximum)


def _normalize_markers(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise SU2MeshAuditError("expected_markers must be an iterable of names")
    markers: list[str] = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip() or raw != raw.strip():
            raise SU2MeshAuditError(
                "expected_markers must contain nonempty, trimmed strings"
            )
        markers.append(raw)
    if not markers:
        raise SU2MeshAuditError("expected_markers must not be empty")
    if len(markers) != len(set(markers)):
        raise SU2MeshAuditError("expected_markers must be unique")
    return tuple(sorted(markers))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_assignment(line: str) -> tuple[str, str] | None:
    if "=" not in line:
        return None
    key, value = (part.strip() for part in line.split("=", 1))
    return key.upper(), value


def _parse_integer(value: str, label: str, *, positive: bool = False) -> int:
    if not value or any(character.isspace() for character in value):
        raise SU2MeshAuditError(f"{label} is not a single integer")
    try:
        result = int(value, 10)
    except ValueError as error:
        raise SU2MeshAuditError(f"{label} is not parseable") from error
    if positive and result <= 0:
        raise SU2MeshAuditError(f"{label} must be positive")
    if not positive and result < 0:
        raise SU2MeshAuditError(f"{label} must be nonnegative")
    return result


class _SU2StateMachine:
    """Strict serial SU2 reader with O(marker-count) auxiliary memory."""

    def __init__(
        self,
        *,
        target_element_range: tuple[int, int],
        expected_markers: tuple[str, ...],
    ) -> None:
        self.minimum_elements, self.maximum_elements = target_element_range
        self.expected_markers = expected_markers
        self.state = "EXPECT_NDIME"
        self.line_number = 0
        self.raw_line_count = 0
        self.record_line_count = 0
        self.seen_counts: set[str] = set()
        self.ndime: int | None = None
        self.nelem: int | None = None
        self.npoin: int | None = None
        self.nmark: int | None = None
        self.remaining_volume_elements = 0
        self.remaining_points = 0
        self.remaining_marker_elements = 0
        self.completed_markers = 0
        self.current_marker: str | None = None
        self.point_index_mode: str | None = None
        self.minimum_volume_node_index: int | None = None
        self.maximum_volume_node_index: int | None = None
        self.volume_element_types: Counter[str] = Counter()
        self.marker_element_counts: dict[str, int] = {}
        self.marker_element_types: dict[str, Counter[str]] = {}
        self.boundary_element_types: Counter[str] = Counter()

    def _fail(self, message: str) -> None:
        raise SU2MeshAuditError(
            message, line_number=self.line_number, state=self.state
        )

    def _assignment(self, line: str, expected: str) -> str:
        assignment = _split_assignment(line)
        if assignment is None:
            self._fail(f"missing {expected} before data record")
        key, value = assignment
        if key in _COUNT_KEYS and key in self.seen_counts:
            self._fail(f"duplicate {key} count")
        if key != expected:
            self._fail(f"missing {expected} before {key or 'assignment'}")
        if not value:
            self._fail(f"{expected} value is empty")
        return value

    def _count(self, line: str, expected: str, *, positive: bool) -> int:
        value = self._assignment(line, expected)
        try:
            result = _parse_integer(value, expected, positive=positive)
        except SU2MeshAuditError as error:
            self._fail(str(error))
        self.seen_counts.add(expected)
        return result

    def _tokens(self, line: str, label: str) -> list[int]:
        try:
            return [int(value, 10) for value in line.split()]
        except ValueError:
            self._fail(f"{label} contains a non-integer field")

    def _reject_duplicate_count(self, line: str) -> None:
        assignment = _split_assignment(line)
        if assignment is not None:
            key, _ = assignment
            if key in _COUNT_KEYS and key in self.seen_counts:
                self._fail(f"duplicate {key} count")

    def _consume_volume(self, line: str) -> None:
        self._reject_duplicate_count(line)
        tokens = self._tokens(line, "volume element")
        if not tokens:
            self._fail("volume element record is empty")
        element_type = tokens[0]
        definition = _VOLUME_TYPES.get(element_type)
        if definition is None:
            self._fail(f"unsupported volume element type {element_type}")
        name, node_count = definition
        if len(tokens) not in {node_count + 1, node_count + 2}:
            self._fail(f"{name} has an invalid field count")
        nodes = tokens[1 : node_count + 1]
        if len(nodes) != len(set(nodes)):
            self._fail(f"{name} contains repeated node indices")
        minimum = min(nodes)
        maximum = max(nodes)
        if minimum < 0:
            self._fail("volume node index must be nonnegative")
        self.minimum_volume_node_index = (
            minimum
            if self.minimum_volume_node_index is None
            else min(self.minimum_volume_node_index, minimum)
        )
        self.maximum_volume_node_index = (
            maximum
            if self.maximum_volume_node_index is None
            else max(self.maximum_volume_node_index, maximum)
        )
        if len(tokens) == node_count + 2 and tokens[-1] < 0:
            self._fail("volume element index must be nonnegative")
        self.volume_element_types[name] += 1
        self.remaining_volume_elements -= 1
        if self.remaining_volume_elements == 0:
            self.state = "EXPECT_NPOIN"

    def _consume_point(self, line: str) -> None:
        self._reject_duplicate_count(line)
        tokens = line.split()
        if len(tokens) not in {3, 4}:
            self._fail("point record must contain three coordinates and optional index")
        for raw in tokens[:3]:
            try:
                coordinate = float(raw)
            except ValueError:
                self._fail("point coordinate is not parseable")
            if not math.isfinite(coordinate):
                self._fail("point coordinate contains NaN or Inf")
        indexed = len(tokens) == 4
        mode = "explicit_sequential" if indexed else "implicit_sequential"
        if self.point_index_mode is None:
            self.point_index_mode = mode
        elif self.point_index_mode != mode:
            self._fail("point records mix explicit and implicit indices")
        point_record_index = int(self.npoin) - self.remaining_points
        if indexed:
            try:
                point_index = int(tokens[3], 10)
            except ValueError:
                self._fail("point index is not parseable")
            if point_index != point_record_index:
                self._fail(
                    "point index is not the canonical sequential record index"
                )
        self.remaining_points -= 1
        if self.remaining_points == 0:
            self.state = "EXPECT_NMARK"

    def _consume_marker_element(self, line: str) -> None:
        self._reject_duplicate_count(line)
        tokens = self._tokens(line, "marker element")
        if not tokens:
            self._fail("marker element record is empty")
        element_type = tokens[0]
        definition = _BOUNDARY_TYPES.get(element_type)
        if definition is None:
            self._fail(f"unsupported marker element type {element_type}")
        name, node_count = definition
        if len(tokens) not in {node_count + 1, node_count + 2}:
            self._fail(f"{name} marker element has an invalid field count")
        nodes = tokens[1 : node_count + 1]
        if len(nodes) != len(set(nodes)):
            self._fail(f"{name} marker element contains repeated node indices")
        assert self.npoin is not None
        invalid = next((node for node in nodes if not 0 <= node < self.npoin), None)
        if invalid is not None:
            self._fail(
                f"marker node index {invalid} is outside [0, {self.npoin})"
            )
        if len(tokens) == node_count + 2 and tokens[-1] < 0:
            self._fail("marker element index must be nonnegative")
        assert self.current_marker is not None
        self.marker_element_types[self.current_marker][name] += 1
        self.boundary_element_types[name] += 1
        self.remaining_marker_elements -= 1
        if self.remaining_marker_elements == 0:
            self.completed_markers += 1
            self.current_marker = None
            assert self.nmark is not None
            self.state = (
                "DONE"
                if self.completed_markers == self.nmark
                else "EXPECT_MARKER_TAG"
            )

    def consume(self, raw_line: str, line_number: int) -> None:
        self.line_number = line_number
        self.raw_line_count = line_number
        if len(raw_line) > _MAX_LINE_CHARACTERS:
            self._fail("SU2 record exceeds the maximum safe line length")
        if _NONFINITE_TEXT.search(raw_line):
            self._fail("SU2 mesh contains NaN or Inf")
        line = raw_line.split("%", 1)[0].strip()
        if not line:
            return
        self.record_line_count += 1

        if self.state == "EXPECT_NDIME":
            self.ndime = self._count(line, "NDIME", positive=True)
            if self.ndime != 3:
                self._fail("NDIME must be 3 for a coarse three-dimensional mesh")
            self.state = "EXPECT_NELEM"
            return
        if self.state == "EXPECT_NELEM":
            self.nelem = self._count(line, "NELEM", positive=True)
            if not self.minimum_elements <= self.nelem <= self.maximum_elements:
                self._fail(
                    "NELEM is outside target element range "
                    f"[{self.minimum_elements}, {self.maximum_elements}]"
                )
            self.remaining_volume_elements = self.nelem
            self.state = "VOLUME_ELEMENTS"
            return
        if self.state == "VOLUME_ELEMENTS":
            self._consume_volume(line)
            return
        if self.state == "EXPECT_NPOIN":
            self.npoin = self._count(line, "NPOIN", positive=True)
            if (
                self.maximum_volume_node_index is None
                or self.minimum_volume_node_index is None
            ):
                self._fail("volume mesh contains no referenced nodes")
            if self.maximum_volume_node_index >= self.npoin:
                self._fail(
                    f"volume node index {self.maximum_volume_node_index} is outside "
                    f"[0, {self.npoin})"
                )
            self.remaining_points = self.npoin
            self.state = "POINTS"
            return
        if self.state == "POINTS":
            self._consume_point(line)
            return
        if self.state == "EXPECT_NMARK":
            self.nmark = self._count(line, "NMARK", positive=True)
            if self.nmark != len(self.expected_markers):
                self._fail(
                    "NMARK and expected marker count differ; markers do not exactly match"
                )
            self.state = "EXPECT_MARKER_TAG"
            return
        if self.state == "EXPECT_MARKER_TAG":
            value = self._assignment(line, "MARKER_TAG")
            if value in self.marker_element_counts:
                self._fail(f"duplicate marker name {value}")
            if value not in self.expected_markers:
                self._fail(
                    f"marker {value!r} is outside the exact marker contract; "
                    "markers do not exactly match"
                )
            self.current_marker = value
            self.state = "EXPECT_MARKER_ELEMS"
            return
        if self.state == "EXPECT_MARKER_ELEMS":
            value = self._assignment(line, "MARKER_ELEMS")
            try:
                count = _parse_integer(value, "marker element count", positive=True)
            except SU2MeshAuditError as error:
                self._fail(str(error))
            assert self.current_marker is not None
            self.marker_element_counts[self.current_marker] = count
            self.marker_element_types[self.current_marker] = Counter()
            self.remaining_marker_elements = count
            self.state = "MARKER_ELEMENTS"
            return
        if self.state == "MARKER_ELEMENTS":
            self._consume_marker_element(line)
            return
        if self.state == "DONE":
            self._reject_duplicate_count(line)
            self._fail("SU2 mesh contains an unexpected trailing record")
        self._fail(f"unknown parser state {self.state}")

    def finish(self) -> None:
        if self.state == "DONE":
            actual = set(self.marker_element_counts)
            expected = set(self.expected_markers)
            if actual != expected:
                self._fail("markers do not exactly match the expected marker contract")
            if any(value <= 0 for value in self.marker_element_counts.values()):
                self._fail("each marker element count must be positive")
            return
        missing_by_state = {
            "EXPECT_NDIME": "missing NDIME",
            "EXPECT_NELEM": "missing NELEM",
            "EXPECT_NPOIN": "missing NPOIN",
            "EXPECT_NMARK": "missing NMARK",
            "EXPECT_MARKER_TAG": "missing MARKER_TAG",
            "EXPECT_MARKER_ELEMS": "missing MARKER_ELEMS",
        }
        if self.state in missing_by_state:
            self._fail(missing_by_state[self.state])
        if self.state == "VOLUME_ELEMENTS":
            self._fail(
                f"SU2 ended with {self.remaining_volume_elements} volume elements missing"
            )
        if self.state == "POINTS":
            self._fail(f"SU2 ended with {self.remaining_points} point records missing")
        if self.state == "MARKER_ELEMENTS":
            self._fail(
                f"SU2 ended with {self.remaining_marker_elements} marker elements missing"
            )
        self._fail(f"SU2 ended in unknown parser state {self.state}")

    def evidence(self) -> dict[str, Any]:
        marker_types = {
            marker: dict(sorted(counts.items()))
            for marker, counts in sorted(self.marker_element_types.items())
        }
        return {
            "ndime": self.ndime,
            "nelem": self.nelem,
            "npoin": self.npoin,
            "nmark": self.nmark,
            "volume_element_types": dict(sorted(self.volume_element_types.items())),
            "boundary_element_types": dict(
                sorted(self.boundary_element_types.items())
            ),
            "marker_element_counts": dict(sorted(self.marker_element_counts.items())),
            "marker_element_types": marker_types,
            "volume_node_index_range": {
                "minimum": self.minimum_volume_node_index,
                "maximum": self.maximum_volume_node_index,
            },
            "point_index_mode": self.point_index_mode,
            "raw_line_count": self.raw_line_count,
            "record_line_count": self.record_line_count,
            "parser_state": self.state,
        }


def audit_su2_mesh(
    path: PathLike,
    *,
    target_element_range: tuple[int, int],
    expected_markers: Iterable[str],
) -> dict[str, Any]:
    """Audit a serialized 3-D SU2 mixed mesh without retaining connectivity.

    The element range is inclusive.  Content, decoding and filesystem failures
    are returned as strict-JSON-serializable ``FAIL`` evidence so a supervising
    pipeline can publish the original reason before stopping downstream work.
    """

    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (TypeError, ValueError, OSError) as error:
        resolved = Path(os.path.abspath(os.fspath(path)))
        path_error: BaseException | None = error
    else:
        path_error = None

    result: dict[str, Any] = {
        "schema": _SCHEMA,
        "status": "FAIL",
        "path": str(resolved),
        "file_size_bytes": None,
        "sha256": None,
        "constraints": {
            "target_element_range": None,
            "expected_markers": None,
            "allowed_volume_element_types": {
                str(code): name for code, (name, _) in sorted(_VOLUME_TYPES.items())
            },
            "allowed_boundary_element_types": {
                str(code): name
                for code, (name, _) in sorted(_BOUNDARY_TYPES.items())
            },
        },
        "ndime": None,
        "nelem": None,
        "npoin": None,
        "nmark": None,
        "volume_element_types": {},
        "boundary_element_types": {},
        "marker_element_counts": {},
        "marker_element_types": {},
        "volume_node_index_range": {"minimum": None, "maximum": None},
        "point_index_mode": None,
        "raw_line_count": 0,
        "record_line_count": 0,
        "parser_state": "NOT_STARTED",
        "bounded_memory": {
            "whole_file_text_loaded": False,
            "connectivity_retained": False,
            "file_passes": 2,
        },
        "error": None,
    }
    parser: _SU2StateMachine | None = None
    try:
        if path_error is not None:
            raise SU2MeshAuditError(f"cannot resolve SU2 mesh path: {path_error}")
        element_range = _normalize_element_range(target_element_range)
        markers = _normalize_markers(expected_markers)
        result["constraints"]["target_element_range"] = {
            "minimum": element_range[0],
            "maximum": element_range[1],
        }
        result["constraints"]["expected_markers"] = list(markers)
        if not resolved.is_file():
            raise SU2MeshAuditError("SU2 mesh is missing or is not a regular file")
        size = resolved.stat().st_size
        result["file_size_bytes"] = size
        result["sha256"] = _sha256(resolved)
        if size <= 0:
            raise SU2MeshAuditError("SU2 mesh is empty")

        parser = _SU2StateMachine(
            target_element_range=element_range,
            expected_markers=markers,
        )
        try:
            with resolved.open("r", encoding="utf-8", errors="strict", newline=None) as stream:
                for line_number, raw_line in enumerate(stream, 1):
                    parser.consume(raw_line, line_number)
        except UnicodeDecodeError as error:
            raise SU2MeshAuditError(
                f"SU2 mesh is not valid UTF-8: {error}",
                line_number=parser.line_number + 1,
                state=parser.state,
            ) from error
        parser.finish()
        result.update(parser.evidence())
        result["status"] = "PASS"
    except (OSError, SU2MeshAuditError) as error:
        if parser is not None:
            result.update(parser.evidence())
        result["status"] = "FAIL"
        result["error"] = {
            "type": type(error).__name__,
            "message": str(error),
            "line_number": getattr(error, "line_number", None),
            "parser_state": getattr(error, "state", None),
        }
    _json_safe(result)
    return result


__all__ = ["SU2MeshAuditError", "audit_su2_mesh"]
