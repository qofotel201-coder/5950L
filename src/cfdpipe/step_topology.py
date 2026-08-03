"""Read-only ISO 10303-21 topology helpers for STEP connection checks.

This module deliberately performs no CAD operations.  It reads STEP text,
catalogues the B-rep shell-to-face graph and offers a conservative comparison
with Gmsh volume boundary counts.  A face-count comparison is evidence for a
mapping only when it is unique on both sides; it is never used to guess among
multiple candidates.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any


PathLike = str | Path


class StepTopologyError(ValueError):
    """Raised when the STEP topology text is incomplete or contradictory."""


@dataclass(frozen=True, slots=True)
class StepRecord:
    """A simple ``#id = TYPE(arguments);`` STEP entity record."""

    entity_id: int
    entity_type: str
    arguments: str
    references: tuple[int, ...]


_ENTITY_START_RE = re.compile(
    r"#\s*(?P<id>\d+)\s*=\s*(?P<type>[A-Za-z_][A-Za-z0-9_]*)\s*\(",
    re.ASCII,
)
_X2_RE = re.compile(r"\\X2\\(?P<hex>[0-9A-Fa-f]*)\\X0\\", re.IGNORECASE)
_SOLID_TYPES = frozenset({"MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS"})
_SHELL_TYPES = frozenset(
    {
        "CLOSED_SHELL",
        "OPEN_SHELL",
        "ORIENTED_CLOSED_SHELL",
        "ORIENTED_OPEN_SHELL",
    }
)
_FACE_WRAPPER_TYPES = frozenset({"ORIENTED_FACE"})


def _skip_quoted_string(text: str, offset: int) -> int:
    """Return the first offset after a STEP single-quoted string."""

    if offset >= len(text) or text[offset] != "'":
        raise AssertionError("quoted string scanner must start on a quote")
    cursor = offset + 1
    while cursor < len(text):
        if text[cursor] != "'":
            cursor += 1
            continue
        if cursor + 1 < len(text) and text[cursor + 1] == "'":
            cursor += 2
            continue
        return cursor + 1
    raise StepTopologyError("unterminated STEP string literal")


def _skip_comment(text: str, offset: int) -> int:
    end = text.find("*/", offset + 2)
    if end < 0:
        raise StepTopologyError("unterminated STEP block comment")
    return end + 2


def _extract_references(arguments: str) -> tuple[int, ...]:
    references: list[int] = []
    cursor = 0
    while cursor < len(arguments):
        if arguments.startswith("/*", cursor):
            cursor = _skip_comment(arguments, cursor)
            continue
        if arguments[cursor] == "'":
            cursor = _skip_quoted_string(arguments, cursor)
            continue
        if arguments[cursor] == "#":
            match = re.match(r"#\s*(\d+)", arguments[cursor:], re.ASCII)
            if match is not None:
                references.append(int(match.group(1)))
                cursor += match.end()
                continue
        cursor += 1
    return tuple(references)


def parse_step_records(text: str) -> dict[int, StepRecord]:
    """Parse all simple, possibly multiline STEP entity records in ``text``.

    Strings and block comments are honoured while locating the closing
    parenthesis and semicolon, so record-like text inside either is ignored.
    Complex entity instances beginning with ``#id = (`` are outside the simple
    record contract and are skipped.
    """

    if not isinstance(text, str):
        raise TypeError("STEP text must be a string")

    records: dict[int, StepRecord] = {}
    cursor = 0
    while cursor < len(text):
        if text.startswith("/*", cursor):
            cursor = _skip_comment(text, cursor)
            continue
        if text[cursor] == "'":
            cursor = _skip_quoted_string(text, cursor)
            continue
        if text[cursor] != "#":
            cursor += 1
            continue

        match = _ENTITY_START_RE.match(text, cursor)
        if match is None:
            cursor += 1
            continue

        entity_id = int(match.group("id"))
        entity_type = match.group("type").upper()
        argument_start = match.end()
        depth = 1
        scan = argument_start
        while scan < len(text) and depth:
            if text.startswith("/*", scan):
                scan = _skip_comment(text, scan)
                continue
            character = text[scan]
            if character == "'":
                scan = _skip_quoted_string(text, scan)
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    break
            scan += 1
        if depth:
            raise StepTopologyError(
                f"unterminated STEP record #{entity_id} ({entity_type})"
            )

        arguments = text[argument_start:scan]
        terminator = scan + 1
        while terminator < len(text):
            if text.startswith("/*", terminator):
                terminator = _skip_comment(text, terminator)
                continue
            if text[terminator].isspace():
                terminator += 1
                continue
            break
        if terminator >= len(text) or text[terminator] != ";":
            raise StepTopologyError(
                f"STEP record #{entity_id} ({entity_type}) is missing its semicolon"
            )
        if entity_id in records:
            raise StepTopologyError(f"duplicate STEP entity id #{entity_id}")
        records[entity_id] = StepRecord(
            entity_id=entity_id,
            entity_type=entity_type,
            arguments=arguments,
            references=_extract_references(arguments),
        )
        cursor = terminator + 1

    return records


def decode_step_string(value: str) -> str:
    """Decode a STEP string, including ``\\X2\\...\\X0\\`` UTF-16BE blocks."""

    if not isinstance(value, str):
        raise TypeError("STEP string value must be a string")
    payload = value
    if len(payload) >= 2 and payload[0] == payload[-1] == "'":
        payload = payload[1:-1]

    def replace_x2(match: re.Match[str]) -> str:
        encoded = match.group("hex")
        if not encoded or len(encoded) % 4:
            raise StepTopologyError(
                "STEP X2 payload must contain complete 16-bit hexadecimal units"
            )
        try:
            return bytes.fromhex(encoded).decode("utf-16-be")
        except (UnicodeDecodeError, ValueError) as error:
            raise StepTopologyError(f"invalid STEP X2 payload {encoded!r}") from error

    decoded = _X2_RE.sub(replace_x2, payload)
    return decoded.replace("''", "'")


def _extract_first_string(arguments: str) -> str:
    cursor = 0
    while cursor < len(arguments):
        if arguments.startswith("/*", cursor):
            cursor = _skip_comment(arguments, cursor)
            continue
        if arguments[cursor] != "'":
            cursor += 1
            continue
        end = _skip_quoted_string(arguments, cursor)
        return decode_step_string(arguments[cursor:end])
    return ""


def _collect_solid_topology(
    solid: StepRecord,
    records: Mapping[int, StepRecord],
) -> dict[str, Any]:
    shell_ids: set[int] = set()
    face_ids: set[int] = set()
    completed: set[int] = set()
    active: list[int] = []

    def walk(entity_id: int) -> None:
        if entity_id in active:
            cycle_start = active.index(entity_id)
            cycle = active[cycle_start:] + [entity_id]
            rendered = " -> ".join(f"#{item}" for item in cycle)
            raise StepTopologyError(
                f"cycle in STEP shell/face topology for solid #{solid.entity_id}: "
                f"{rendered}"
            )
        if entity_id in completed:
            return
        record = records.get(entity_id)
        if record is None:
            raise StepTopologyError(
                f"solid #{solid.entity_id} references missing STEP entity #{entity_id}"
            )
        if record.entity_type == "ADVANCED_FACE":
            face_ids.add(entity_id)
            completed.add(entity_id)
            return
        if record.entity_type not in _SHELL_TYPES | _FACE_WRAPPER_TYPES:
            raise StepTopologyError(
                f"solid #{solid.entity_id} topology references unexpected "
                f"{record.entity_type} entity #{entity_id}"
            )

        if record.entity_type in _SHELL_TYPES:
            shell_ids.add(entity_id)
        active.append(entity_id)
        try:
            if not record.references:
                raise StepTopologyError(
                    f"{record.entity_type} entity #{entity_id} has no referenced faces "
                    "or shells"
                )
            for reference in record.references:
                walk(reference)
        finally:
            active.pop()
        completed.add(entity_id)

    if not solid.references:
        raise StepTopologyError(
            f"{solid.entity_type} entity #{solid.entity_id} has no shell reference"
        )
    for reference in solid.references:
        walk(reference)
    if not shell_ids:
        raise StepTopologyError(
            f"{solid.entity_type} entity #{solid.entity_id} contains no shell"
        )
    if not face_ids:
        raise StepTopologyError(
            f"{solid.entity_type} entity #{solid.entity_id} contains no ADVANCED_FACE"
        )

    def component_for(root_id: int, role: str) -> dict[str, Any]:
        component_shells: set[int] = set()
        component_faces: set[int] = set()
        component_active: list[int] = []

        def collect(entity_id: int) -> None:
            if entity_id in component_active:
                rendered = " -> ".join(
                    f"#{item}" for item in (*component_active, entity_id)
                )
                raise StepTopologyError(
                    f"cycle in STEP root shell component for solid "
                    f"#{solid.entity_id}: {rendered}"
                )
            record = records.get(entity_id)
            if record is None:
                raise StepTopologyError(
                    f"solid #{solid.entity_id} root shell references missing "
                    f"STEP entity #{entity_id}"
                )
            if record.entity_type == "ADVANCED_FACE":
                component_faces.add(entity_id)
                return
            if record.entity_type not in _SHELL_TYPES | _FACE_WRAPPER_TYPES:
                raise StepTopologyError(
                    f"solid #{solid.entity_id} root shell references unexpected "
                    f"{record.entity_type} entity #{entity_id}"
                )
            if record.entity_type in _SHELL_TYPES:
                component_shells.add(entity_id)
            component_active.append(entity_id)
            try:
                for reference in record.references:
                    collect(reference)
            finally:
                component_active.pop()

        collect(root_id)
        return {
            "root_shell_id": root_id,
            "role_in_solid": role,
            "shell_ids": sorted(component_shells),
            "advanced_face_ids": sorted(component_faces),
            "face_count": len(component_faces),
        }

    root_shell_ids = list(dict.fromkeys(int(value) for value in solid.references))
    shell_components = []
    for index, root_shell_id in enumerate(root_shell_ids):
        if solid.entity_type == "BREP_WITH_VOIDS":
            role = "outer_shell" if index == 0 else "void_shell"
        else:
            role = "boundary_shell"
        shell_components.append(component_for(root_shell_id, role))

    ordered_faces = sorted(face_ids)
    return {
        "id": solid.entity_id,
        "type": solid.entity_type,
        "name": _extract_first_string(solid.arguments),
        "root_shell_ids": root_shell_ids,
        "shell_ids": sorted(shell_ids),
        "shell_components": shell_components,
        "advanced_face_ids": ordered_faces,
        "face_count": len(ordered_faces),
    }


def parse_step_topology(step_path: PathLike) -> dict[str, Any]:
    """Read ``step_path`` and return its solid/shell/ADVANCED_FACE topology."""

    source = Path(step_path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise StepTopologyError(f"STEP source is not a regular file: {source}")
    data = source.read_bytes()
    source_sha256 = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8-sig")
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        text = data.decode("latin-1")
        encoding = "latin-1"

    records = parse_step_records(text)
    if not records:
        raise StepTopologyError(f"no simple STEP entity records found in {source}")
    solid_records = sorted(
        (record for record in records.values() if record.entity_type in _SOLID_TYPES),
        key=lambda record: record.entity_id,
    )
    if not solid_records:
        raise StepTopologyError(f"no B-rep solid records found in {source}")
    solids = [_collect_solid_topology(record, records) for record in solid_records]
    return {
        "source_path": str(source),
        "source_sha256": source_sha256,
        "source_encoding": encoding,
        "record_count": len(records),
        "solid_count": len(solids),
        "solids": solids,
    }


def _require_plain_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StepTopologyError(f"{field_name} must be an integer")
    return value


def map_step_solids_to_gmsh_volumes(
    topology: Mapping[str, Any],
    volume_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Match STEP solids to Gmsh volumes only by unique boundary face count.

    A count group is ``MATCHED`` only when it contains exactly one STEP solid
    and exactly one Gmsh volume.  Repeated counts are reported as ``AMBIGUOUS``;
    absent counts are ``UNMATCHED``.  The function never chooses among tied
    candidates.
    """

    if not isinstance(topology, Mapping):
        raise TypeError("topology must be a mapping")
    raw_solids = topology.get("solids")
    if not isinstance(raw_solids, Sequence) or isinstance(raw_solids, (str, bytes)):
        raise StepTopologyError("topology.solids must be a sequence")
    if not isinstance(volume_records, Sequence) or isinstance(
        volume_records, (str, bytes)
    ):
        raise TypeError("volume_records must be a sequence of mappings")

    solids: list[dict[str, Any]] = []
    seen_solid_ids: set[int] = set()
    for index, raw_solid in enumerate(raw_solids):
        if not isinstance(raw_solid, Mapping):
            raise StepTopologyError(f"topology.solids[{index}] must be a mapping")
        solid_id = _require_plain_integer(raw_solid.get("id"), f"solid[{index}].id")
        face_count = _require_plain_integer(
            raw_solid.get("face_count"), f"solid #{solid_id} face_count"
        )
        if solid_id <= 0 or face_count <= 0:
            raise StepTopologyError("solid ids and face counts must be positive")
        if solid_id in seen_solid_ids:
            raise StepTopologyError(f"duplicate STEP solid id #{solid_id}")
        seen_solid_ids.add(solid_id)
        solids.append(
            {
                "solid_id": solid_id,
                "solid_type": str(raw_solid.get("type", "")),
                "solid_name": str(raw_solid.get("name", "")),
                "face_count": face_count,
            }
        )

    volumes: list[dict[str, Any]] = []
    seen_volume_tags: set[int] = set()
    for index, raw_volume in enumerate(volume_records):
        if not isinstance(raw_volume, Mapping):
            raise StepTopologyError(f"volume_records[{index}] must be a mapping")
        entity_tag = _require_plain_integer(
            raw_volume.get("entity_tag"), f"volume[{index}].entity_tag"
        )
        raw_surfaces = raw_volume.get("boundary_surfaces")
        if not isinstance(raw_surfaces, Sequence) or isinstance(
            raw_surfaces, (str, bytes)
        ):
            raise StepTopologyError(
                f"volume {entity_tag} boundary_surfaces must be a sequence"
            )
        if entity_tag <= 0:
            raise StepTopologyError("volume entity tags must be positive")
        if entity_tag in seen_volume_tags:
            raise StepTopologyError(f"duplicate Gmsh volume entity tag {entity_tag}")
        seen_volume_tags.add(entity_tag)
        surface_tags = [
            _require_plain_integer(value, f"volume {entity_tag} boundary surface")
            for value in raw_surfaces
        ]
        if any(tag <= 0 for tag in surface_tags):
            raise StepTopologyError("boundary surface tags must be positive")
        if not surface_tags:
            raise StepTopologyError(
                f"Gmsh volume {entity_tag} has no boundary surfaces"
            )
        if len(set(surface_tags)) != len(surface_tags):
            raise StepTopologyError(
                f"Gmsh volume {entity_tag} contains duplicate boundary surfaces"
            )
        volumes.append(
            {
                "volume_entity_tag": entity_tag,
                "boundary_surfaces": sorted(surface_tags),
                "face_count": len(surface_tags),
            }
        )

    solids_by_count: dict[int, list[dict[str, Any]]] = defaultdict(list)
    volumes_by_count: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for solid in solids:
        solids_by_count[solid["face_count"]].append(solid)
    for volume in volumes:
        volumes_by_count[volume["face_count"]].append(volume)

    solid_results: list[dict[str, Any]] = []
    volume_results: list[dict[str, Any]] = []
    matches: list[dict[str, int]] = []
    for face_count in sorted(set(solids_by_count) | set(volumes_by_count)):
        count_solids = sorted(
            solids_by_count.get(face_count, []), key=lambda item: item["solid_id"]
        )
        count_volumes = sorted(
            volumes_by_count.get(face_count, []),
            key=lambda item: item["volume_entity_tag"],
        )
        if len(count_solids) == len(count_volumes) == 1:
            group_status = "MATCHED"
            solid_id = count_solids[0]["solid_id"]
            volume_tag = count_volumes[0]["volume_entity_tag"]
            matches.append(
                {
                    "solid_id": solid_id,
                    "volume_entity_tag": volume_tag,
                    "face_count": face_count,
                }
            )
        elif count_solids and count_volumes:
            group_status = "AMBIGUOUS"
            solid_id = None
            volume_tag = None
        else:
            group_status = "UNMATCHED"
            solid_id = None
            volume_tag = None

        candidate_volume_tags = [
            item["volume_entity_tag"] for item in count_volumes
        ]
        candidate_solid_ids = [item["solid_id"] for item in count_solids]
        for solid in count_solids:
            solid_results.append(
                {
                    **solid,
                    "status": group_status,
                    "candidate_volume_entity_tags": candidate_volume_tags,
                    "volume_entity_tag": volume_tag,
                }
            )
        for volume in count_volumes:
            volume_results.append(
                {
                    **volume,
                    "status": group_status,
                    "candidate_solid_ids": candidate_solid_ids,
                    "solid_id": solid_id,
                }
            )

    statuses = {
        result["status"] for result in (*solid_results, *volume_results)
    }
    if statuses == {"MATCHED"} and len(matches) == len(solids) == len(volumes):
        overall_status = "MATCHED"
    elif "AMBIGUOUS" in statuses:
        overall_status = "AMBIGUOUS"
    else:
        overall_status = "UNMATCHED"

    return {
        "status": overall_status,
        "source_sha256": topology.get("source_sha256"),
        "matches": sorted(matches, key=lambda item: item["solid_id"]),
        "solid_results": sorted(solid_results, key=lambda item: item["solid_id"]),
        "volume_results": sorted(
            volume_results, key=lambda item: item["volume_entity_tag"]
        ),
    }


__all__ = [
    "StepRecord",
    "StepTopologyError",
    "decode_step_string",
    "map_step_solids_to_gmsh_volumes",
    "parse_step_records",
    "parse_step_topology",
]
