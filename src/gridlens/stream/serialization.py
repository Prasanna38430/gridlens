"""Avro on the wire, with the schema held by Redpanda's schema registry.

A message value is the Confluent wire format: one zero byte, the schema id as
four big-endian bytes, then the Avro body with no schema of its own. Five bytes
of header rather than the whole schema in every message, and any consumer can
fetch the schema by id. Written out here rather than through a registry client
library, because it is five bytes and the library would be most of the image.
"""

from __future__ import annotations

import io
import json
import struct
from decimal import Decimal
from importlib import resources
from typing import Any, Protocol

import fastavro

from gridlens.contracts.records import GenerationRecord

MAGIC = 0
HEADER = struct.Struct(">bI")

SCHEMA_TEXT = (
    resources.files("gridlens.stream")
    .joinpath("generation_observation.avsc")
    .read_text(encoding="utf-8")
)
SCHEMA = fastavro.parse_schema(json.loads(SCHEMA_TEXT))

# avro decimals carry a fixed scale, and the contract allows up to three places
QUANTUM = Decimal("0.001")


class WireFormatError(ValueError):
    pass


def key(record: GenerationRecord) -> bytes:
    """One series per key, so every value of a series lands in one partition.

    Kafka only orders messages within a partition. Keying by series means a
    consumer sees each series' observations in the order they were produced,
    which is what a revision needs: the later one arrives later.
    """
    return "|".join(
        (
            record.source,
            record.zone.value,
            record.production_type.value,
            record.direction,
        )
    ).encode("utf-8")


def to_avro(record: GenerationRecord) -> dict[str, Any]:
    return {
        "source": record.source,
        "source_document_id": record.source_document_id,
        "zone": record.zone.value,
        "production_type": record.production_type.value,
        "direction": record.direction,
        "unit": record.unit,
        "resolution_minutes": record.resolution_minutes,
        "valid_time": record.valid_time,
        "known_at": record.known_at,
        "source_updated_at": record.source_updated_at,
        "quantity_mw": record.quantity_mw.quantize(QUANTUM),
    }


def encode(record: GenerationRecord, schema_id: int) -> bytes:
    body = io.BytesIO()
    body.write(HEADER.pack(MAGIC, schema_id))
    fastavro.schemaless_writer(body, SCHEMA, to_avro(record))
    return body.getvalue()


def decode(payload: bytes) -> tuple[int, dict[str, Any]]:
    """The schema id and the record, for tests and for reading a topic back."""
    if len(payload) < HEADER.size:
        raise WireFormatError(f"{len(payload)} bytes is shorter than the header")
    magic, schema_id = HEADER.unpack_from(payload)
    if magic != MAGIC:
        raise WireFormatError(f"magic byte {magic}, expected {MAGIC}")
    record = fastavro.schemaless_reader(io.BytesIO(payload[HEADER.size :]), SCHEMA)
    if not isinstance(record, dict):
        raise WireFormatError("the body did not decode to a record")
    return schema_id, record


class HttpClient(Protocol):
    def post(self, url: str, /, *, headers: dict[str, str], json: Any) -> Any: ...
    def put(self, url: str, /, *, headers: dict[str, str], json: Any) -> Any: ...


def register(http: HttpClient, registry_url: str, subject: str) -> int:
    """Register the schema under a subject and return its id.

    Registering a schema the subject already has returns the existing id, so
    this runs on every start. The subject is set to BACKWARD compatibility
    first, so a later version that a consumer on this one could not read is
    refused by the registry instead of breaking the consumer.
    """
    headers = {"Content-Type": "application/vnd.schemaregistry.v1+json"}
    http.put(
        f"{registry_url}/config/{subject}",
        headers=headers,
        json={"compatibility": "BACKWARD"},
    ).raise_for_status()
    response = http.post(
        f"{registry_url}/subjects/{subject}/versions",
        headers=headers,
        json={"schema": SCHEMA_TEXT},
    )
    response.raise_for_status()
    schema_id: int = response.json()["id"]
    return schema_id
