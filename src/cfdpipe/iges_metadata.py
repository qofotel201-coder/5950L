"""Read-only, standard-library metadata inspection for strict 80-column IGES.

The parser deliberately does not construct CAD geometry.  It records the
global metadata, directory-entry labels and the explicit IGES property/group
entities that can be used as naming evidence before a geometry tool is run.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence


_SECTION_ORDER = ("S", "G", "D", "P", "T")
_GLOBAL_FIELDS = {
    "product_identifier_sender": 2,
    "iges_file_name": 3,
    "native_system_id": 4,
    "preprocessor_version": 5,
    "product_identifier_receiver": 11,
    "model_space_scale": 12,
    "units_flag": 13,
    "units_name": 14,
    "generation_time": 17,
    "minimum_resolution": 18,
    "maximum_coordinate": 19,
    "author": 20,
    "organization": 21,
    "iges_version": 22,
    "drafting_standard": 23,
    "modification_time": 24,
}


class IgesMetadataError(RuntimeError):
    """Raised when an IGES file fails a structural or immutability check."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_read_only(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", None)
    read_only_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", None)
    if attributes is not None and read_only_flag is not None:
        return bool(attributes & read_only_flag)
    writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    return not bool(stat_result.st_mode & writable)


def _snapshot(path: Path) -> dict[str, Any]:
    details = path.stat()
    return {
        "size_bytes": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(
            details.st_mtime, timezone.utc
        ).isoformat().replace("+00:00", "Z"),
        "read_only": _is_read_only(details),
        "sha256": _sha256(path),
    }


def _source_unchanged(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> bool:
    keys = ("size_bytes", "mtime_ns", "read_only", "sha256")
    return all(before[key] == after[key] for key in keys)


def _physical_records(raw: bytes) -> list[bytes]:
    records = raw.splitlines()
    if not records:
        raise IgesMetadataError("IGES file is empty")
    for index, record in enumerate(records, start=1):
        if len(record) != 80:
            raise IgesMetadataError(
                f"IGES physical record {index} has {len(record)} columns; expected 80"
            )
    return records


def _ascii_integer(
    value: bytes,
    *,
    field: str,
    allow_blank: bool = True,
) -> int:
    try:
        text = value.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise IgesMetadataError(f"{field} is not ASCII") from error
    if not text:
        if allow_blank:
            return 0
        raise IgesMetadataError(f"{field} is blank")
    try:
        return int(text)
    except ValueError as error:
        raise IgesMetadataError(f"{field} is not an integer: {text!r}") from error


def _section_records(records: Sequence[bytes]) -> dict[str, list[dict[str, Any]]]:
    sections: dict[str, list[dict[str, Any]]] = {
        name: [] for name in _SECTION_ORDER
    }
    observed_runs: list[str] = []
    previous: str | None = None
    for physical_index, record in enumerate(records, start=1):
        try:
            section = record[72:73].decode("ascii")
        except UnicodeDecodeError as error:
            raise IgesMetadataError(
                f"record {physical_index} has a non-ASCII section code"
            ) from error
        if section not in sections:
            raise IgesMetadataError(
                f"record {physical_index} has invalid section code {section!r}"
            )
        sequence = _ascii_integer(
            record[73:80],
            field=f"record {physical_index} sequence number",
            allow_blank=False,
        )
        expected = len(sections[section]) + 1
        if sequence != expected:
            raise IgesMetadataError(
                f"section {section} sequence is {sequence}; expected {expected}"
            )
        sections[section].append(
            {
                "physical_index": physical_index,
                "sequence": sequence,
                "record": record,
            }
        )
        if section != previous:
            observed_runs.append(section)
            previous = section

    if tuple(observed_runs) != _SECTION_ORDER:
        raise IgesMetadataError(
            "IGES sections must be contiguous and ordered S/G/D/P/T; "
            f"observed {'/'.join(observed_runs)}"
        )
    if len(sections["T"]) != 1:
        raise IgesMetadataError("IGES must contain exactly one terminate record")
    if len(sections["D"]) % 2:
        raise IgesMetadataError("IGES directory section must contain record pairs")
    return sections


def _terminate_counts(
    sections: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, int]:
    terminate = sections["T"][0]["record"]
    result: dict[str, int] = {}
    for index, section in enumerate(("S", "G", "D", "P")):
        field = terminate[index * 8 : (index + 1) * 8]
        try:
            field_section = field[:1].decode("ascii")
        except UnicodeDecodeError as error:
            raise IgesMetadataError("terminate count field is not ASCII") from error
        if field_section != section:
            raise IgesMetadataError(
                f"terminate record field {index + 1} names {field_section!r}; "
                f"expected {section!r}"
            )
        count = _ascii_integer(
            field[1:],
            field=f"terminate {section} count",
            allow_blank=False,
        )
        actual = len(sections[section])
        if count != actual:
            raise IgesMetadataError(
                f"terminate {section} count is {count}; actual count is {actual}"
            )
        result[section] = count
    return result


def _first_hollerith(data: bytes, offset: int) -> tuple[bytes, int]:
    start = offset
    while offset < len(data) and 48 <= data[offset] <= 57:
        offset += 1
    if offset == start or offset >= len(data) or data[offset] not in (72, 104):
        raise IgesMetadataError("global delimiter definition is not Hollerith text")
    length = int(data[start:offset])
    value_start = offset + 1
    value_end = value_start + length
    if value_end > len(data):
        raise IgesMetadataError("truncated global Hollerith delimiter")
    return data[value_start:value_end], value_end


def _global_delimiters(data: bytes) -> tuple[int, int]:
    parameter, offset = _first_hollerith(data, 0)
    if len(parameter) != 1:
        raise IgesMetadataError("IGES parameter delimiter must be one byte")
    if offset >= len(data) or data[offset] != parameter[0]:
        raise IgesMetadataError("missing separator after parameter delimiter")
    record, offset = _first_hollerith(data, offset + 1)
    if len(record) != 1:
        raise IgesMetadataError("IGES record delimiter must be one byte")
    if parameter == record:
        raise IgesMetadataError("IGES parameter and record delimiters must differ")
    if offset >= len(data) or data[offset] != parameter[0]:
        raise IgesMetadataError("missing separator after record delimiter")
    return parameter[0], record[0]


def _split_parameters(
    data: bytes,
    parameter_delimiter: int,
    record_delimiter: int,
    *,
    context: str,
) -> list[bytes]:
    values: list[bytes] = []
    current = bytearray()
    index = 0
    terminated = False
    while index < len(data):
        value = data[index]
        if 48 <= value <= 57:
            end_digits = index
            while end_digits < len(data) and 48 <= data[end_digits] <= 57:
                end_digits += 1
            if end_digits < len(data) and data[end_digits] in (72, 104):
                length = int(data[index:end_digits])
                literal_start = end_digits + 1
                literal_end = literal_start + length
                if literal_end > len(data):
                    raise IgesMetadataError(
                        f"{context} contains truncated Hollerith text"
                    )
                current.extend(data[literal_start:literal_end])
                index = literal_end
                continue
        if value == parameter_delimiter:
            values.append(bytes(current).strip())
            current.clear()
            index += 1
            continue
        if value == record_delimiter:
            values.append(bytes(current).strip())
            terminated = True
            break
        current.append(value)
        index += 1
    if not terminated:
        raise IgesMetadataError(f"{context} lacks its record delimiter")
    return values


def _select_encoding(data: bytes, requested: str | None) -> str:
    candidates = (requested,) if requested else ("utf-8", "gb18030", "latin-1")
    for encoding in candidates:
        if not encoding:
            continue
        try:
            data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        return encoding
    if requested:
        raise IgesMetadataError(
            f"global section cannot be decoded with {requested!r}"
        )
    return "latin-1"


def _decode(value: bytes, encoding: str) -> str:
    return value.decode(encoding, errors="replace").strip()


def _optional_parameter(parameters: Sequence[str], index: int) -> str | None:
    if index >= len(parameters):
        return None
    value = parameters[index]
    return value if value else None


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _global_metadata(
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
    requested_encoding: str | None,
) -> tuple[dict[str, Any], int, int, str]:
    stream = b"".join(item["record"][:72] for item in sections["G"])
    parameter_delimiter, record_delimiter = _global_delimiters(stream)
    raw_parameters = _split_parameters(
        stream,
        parameter_delimiter,
        record_delimiter,
        context="global section",
    )
    encoding = _select_encoding(stream, requested_encoding)
    parameters = [_decode(value, encoding) for value in raw_parameters]
    values = {
        key: _optional_parameter(parameters, index)
        for key, index in _GLOBAL_FIELDS.items()
    }
    values["units_flag"] = _optional_int(values["units_flag"])
    values["iges_version"] = _optional_int(values["iges_version"])
    values["drafting_standard"] = _optional_int(values["drafting_standard"])
    values["parameters"] = parameters
    return values, parameter_delimiter, record_delimiter, encoding


def _directory_entries(
    sections: Mapping[str, Sequence[Mapping[str, Any]]],
    encoding: str,
) -> list[dict[str, Any]]:
    records = sections["D"]
    entries: list[dict[str, Any]] = []
    for offset in range(0, len(records), 2):
        first_item = records[offset]
        second_item = records[offset + 1]
        first = first_item["record"]
        second = second_item["record"]
        if first_item["sequence"] % 2 != 1:
            raise IgesMetadataError("directory entry must begin on an odd sequence")
        if second_item["sequence"] != first_item["sequence"] + 1:
            raise IgesMetadataError("directory entry records are not consecutive")
        first_fields = [first[index : index + 8] for index in range(0, 72, 8)]
        second_fields = [second[index : index + 8] for index in range(0, 72, 8)]
        entity_type = _ascii_integer(
            first_fields[0], field="directory entity type", allow_blank=False
        )
        repeated_type = _ascii_integer(
            second_fields[0],
            field="repeated directory entity type",
            allow_blank=False,
        )
        if repeated_type != entity_type:
            raise IgesMetadataError(
                f"directory entry {first_item['sequence']} repeats entity type "
                f"{repeated_type}; expected {entity_type}"
            )
        entries.append(
            {
                "directory_sequence": first_item["sequence"],
                "entity_type": entity_type,
                "parameter_data_pointer": _ascii_integer(
                    first_fields[1], field="parameter data pointer"
                ),
                "structure": _ascii_integer(first_fields[2], field="structure"),
                "line_font_pattern": _ascii_integer(
                    first_fields[3], field="line font pattern"
                ),
                "level": _ascii_integer(first_fields[4], field="level"),
                "view": _ascii_integer(first_fields[5], field="view"),
                "transformation_matrix": _ascii_integer(
                    first_fields[6], field="transformation matrix"
                ),
                "label_display_associativity": _ascii_integer(
                    first_fields[7], field="label display associativity"
                ),
                "status_number": first_fields[8].decode("ascii").strip(),
                "line_weight": _ascii_integer(
                    second_fields[1], field="line weight"
                ),
                "color": _ascii_integer(second_fields[2], field="color"),
                "parameter_line_count": _ascii_integer(
                    second_fields[3], field="parameter line count"
                ),
                "form_number": _ascii_integer(
                    second_fields[4], field="form number"
                ),
                "entity_label": _decode(second_fields[7], encoding),
                "entity_subscript": _ascii_integer(
                    second_fields[8], field="entity subscript"
                ),
            }
        )
    return entries


def _parameter_payload(
    entry: Mapping[str, Any],
    parameter_records: Sequence[Mapping[str, Any]],
) -> bytes:
    pointer = int(entry["parameter_data_pointer"])
    line_count = int(entry["parameter_line_count"])
    if line_count == 0:
        if pointer != 0:
            raise IgesMetadataError(
                f"directory entry {entry['directory_sequence']} has a parameter "
                "pointer but zero parameter lines"
            )
        return b""
    if pointer < 1 or pointer + line_count - 1 > len(parameter_records):
        raise IgesMetadataError(
            f"directory entry {entry['directory_sequence']} has an invalid "
            "parameter-data span"
        )
    selected = parameter_records[pointer - 1 : pointer - 1 + line_count]
    chunks: list[bytes] = []
    for expected_sequence, item in enumerate(selected, start=pointer):
        if item["sequence"] != expected_sequence:
            raise IgesMetadataError("parameter records are not consecutive")
        record = item["record"]
        directory_pointer = _ascii_integer(
            record[64:72], field="parameter directory pointer", allow_blank=False
        )
        if directory_pointer != entry["directory_sequence"]:
            raise IgesMetadataError(
                f"parameter record {item['sequence']} points to directory entry "
                f"{directory_pointer}; expected {entry['directory_sequence']}"
            )
        chunks.append(record[:64])
    return b"".join(chunks)


def _evidence_record(
    entry: Mapping[str, Any],
    payload: bytes,
    *,
    parameter_delimiter: int,
    record_delimiter: int,
    encoding: str,
) -> dict[str, Any]:
    tokens = _split_parameters(
        payload,
        parameter_delimiter,
        record_delimiter,
        context=f"entity {entry['directory_sequence']} parameter data",
    )
    return {
        "directory_sequence": entry["directory_sequence"],
        "entity_type": entry["entity_type"],
        "form_number": entry["form_number"],
        "entity_label": entry["entity_label"],
        "entity_subscript": entry["entity_subscript"],
        "level": entry["level"],
        "color": entry["color"],
        "parameter_data": _decode(payload, encoding),
        "parameter_tokens": [_decode(value, encoding) for value in tokens],
    }


def _evidence_group(
    entries: Sequence[Mapping[str, Any]],
    payloads: Mapping[int, bytes],
    *,
    entity_type: int,
    parameter_delimiter: int,
    record_delimiter: int,
    encoding: str,
) -> dict[str, Any]:
    selected = [entry for entry in entries if entry["entity_type"] == entity_type]
    records = [
        _evidence_record(
            entry,
            payloads[int(entry["directory_sequence"])],
            parameter_delimiter=parameter_delimiter,
            record_delimiter=record_delimiter,
            encoding=encoding,
        )
        for entry in selected
    ]
    form_counts = Counter(int(entry["form_number"]) for entry in selected)
    return {
        "count": len(records),
        "form_counts": {
            str(key): value for key, value in sorted(form_counts.items())
        },
        "entities": records,
    }


def _counter(values: Sequence[int]) -> dict[str, int]:
    counts = Counter(values)
    return {str(key): value for key, value in sorted(counts.items())}


def _build_metadata(
    source: Path,
    raw: bytes,
    before: Mapping[str, Any],
    *,
    requested_encoding: str | None,
    output_path: Path | None,
) -> dict[str, Any]:
    records = _physical_records(raw)
    sections = _section_records(records)
    terminate = _terminate_counts(sections)
    global_values, parameter_delimiter, record_delimiter, encoding = (
        _global_metadata(sections, requested_encoding)
    )
    entries = _directory_entries(sections, encoding)
    payloads: dict[int, bytes] = {}
    for entry in entries:
        payloads[int(entry["directory_sequence"])] = _parameter_payload(
            entry, sections["P"]
        )

    labels = [
        {
            "directory_sequence": entry["directory_sequence"],
            "entity_type": entry["entity_type"],
            "form_number": entry["form_number"],
            "label": entry["entity_label"],
            "subscript": entry["entity_subscript"],
        }
        for entry in entries
        if entry["entity_label"]
    ]
    type_counts = _counter([int(entry["entity_type"]) for entry in entries])
    properties = _evidence_group(
        entries,
        payloads,
        entity_type=406,
        parameter_delimiter=parameter_delimiter,
        record_delimiter=record_delimiter,
        encoding=encoding,
    )
    groups = _evidence_group(
        entries,
        payloads,
        entity_type=402,
        parameter_delimiter=parameter_delimiter,
        record_delimiter=record_delimiter,
        encoding=encoding,
    )
    subfigures = _evidence_group(
        entries,
        payloads,
        entity_type=308,
        parameter_delimiter=parameter_delimiter,
        record_delimiter=record_delimiter,
        encoding=encoding,
    )
    color_definitions = _evidence_group(
        entries,
        payloads,
        entity_type=314,
        parameter_delimiter=parameter_delimiter,
        record_delimiter=record_delimiter,
        encoding=encoding,
    )
    after = _snapshot(source)
    unchanged = _source_unchanged(before, after)
    if not unchanged:
        raise IgesMetadataError("IGES source changed while it was being inspected")

    section_counts = {name: len(sections[name]) for name in _SECTION_ORDER}
    return {
        "schema_version": 1,
        "status": "PASS",
        "source": {
            "path": str(source),
            "before": dict(before),
            "after": after,
            "unchanged": unchanged,
        },
        "output_path": str(output_path) if output_path is not None else None,
        "format": {
            "record_width": 80,
            "physical_record_count": len(records),
            "section_counts": section_counts,
            "terminate_counts": terminate,
            "parameter_delimiter": chr(parameter_delimiter),
            "record_delimiter": chr(record_delimiter),
            "text_encoding": encoding,
            "start_section_text": _decode(
                b"".join(item["record"][:72] for item in sections["S"]),
                encoding,
            ),
        },
        "global_metadata": global_values,
        "directory": {
            "entity_count": len(entries),
            "entity_type_counts": type_counts,
            "level_counts": _counter([int(entry["level"]) for entry in entries]),
            "color_counts": _counter([int(entry["color"]) for entry in entries]),
            "labeled_entity_count": len(labels),
            "entity_labels": labels,
            "entities": entries,
        },
        "semantic_evidence": {
            "type_406_properties": properties,
            "type_402_groups": groups,
            "type_308_subfigure_definitions": subfigures,
            "type_314_color_definitions": color_definitions,
            "has_property_or_group_evidence": bool(
                properties["count"] or groups["count"] or subfigures["count"]
            ),
        },
    }


def inspect_iges_metadata(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str] | None = None,
    *,
    text_encoding: str | None = None,
) -> dict[str, Any]:
    """Inspect *source_path* without modifying it and optionally write JSON.

    An output is written only when ``output_path`` is explicitly supplied.  To
    prevent accidental loss of prior evidence, an existing output is never
    overwritten.
    """

    source = Path(source_path).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in {".igs", ".iges"}:
        raise IgesMetadataError(f"source is not an IGES file: {source}")

    output: Path | None = None
    if output_path is not None:
        output = Path(output_path).resolve(strict=False)
        if output.suffix.lower() != ".json":
            raise IgesMetadataError("explicit IGES metadata output must be .json")
        if output.exists():
            if output.is_dir():
                raise IgesMetadataError(f"output is a directory: {output}")
            try:
                same_file = os.path.samefile(source, output)
            except OSError:
                same_file = output == source
            if same_file:
                raise IgesMetadataError("output must not be the IGES source")
            raise IgesMetadataError(f"refusing to overwrite existing output: {output}")
        if output == source:
            raise IgesMetadataError("output must not be the IGES source")

    before = _snapshot(source)
    raw = source.read_bytes()
    metadata = _build_metadata(
        source,
        raw,
        before,
        requested_encoding=text_encoding,
        output_path=output,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    return metadata


__all__ = ["IgesMetadataError", "inspect_iges_metadata"]
