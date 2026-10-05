"""The topics this project owns, declared once and created if missing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

DAY_MS = 24 * 60 * 60 * 1000


@dataclass(frozen=True)
class Topic:
    name: str
    partitions: int
    config: dict[str, str] = field(default_factory=dict)


# Three partitions for fifteen series. Each series stays in one partition, so
# its order holds, and three lets day 23's consumer read with more than one
# task without the broker juggling partitions it has no use for on one core.
#
# Delete, not compact. Compaction keeps the newest value per key, and the key
# is a series, so it would throw away every period but the last. Even keyed by
# period it would throw away the revisions, which are the point.
#
# Fourteen days, because the laptop this runs on is not always on, and a
# consumer that was off for a week has to be able to catch up from the topic
# rather than from somewhere else.
OBSERVATIONS = Topic(
    "gridlens.entsoe.generation.v1",
    partitions=3,
    config={"cleanup.policy": "delete", "retention.ms": str(14 * DAY_MS)},
)

# Rows the contract refused, never dropped, the same rule as the quarantine
# bucket. Json rather than avro, because a refused row is by definition one
# that does not fit the schema.
QUARANTINE = Topic(
    "gridlens.entsoe.generation.quarantine.v1",
    partitions=1,
    config={"cleanup.policy": "delete", "retention.ms": str(30 * DAY_MS)},
)

ALL = (OBSERVATIONS, QUARANTINE)

ALREADY_EXISTS = 36  # TOPIC_ALREADY_EXISTS in the kafka protocol


class Admin(Protocol):
    def create_topics(self, new_topics: list[Any], **kwargs: Any) -> dict[str, Any]: ...


def ensure(admin: Admin, new_topic: Any, topics: tuple[Topic, ...] = ALL) -> list[str]:
    """Create whichever topics are missing and return their names.

    new_topic is confluent_kafka's NewTopic, passed in so this module does not
    import the client and the tests do not need a broker. An existing topic is
    left exactly as it is: changing partitions or retention on a live topic is
    a deliberate act, not something a restart should do.
    """
    futures = admin.create_topics(
        [
            new_topic(
                t.name,
                num_partitions=t.partitions,
                replication_factor=1,
                config=t.config,
            )
            for t in topics
        ]
    )
    created = []
    for name, future in futures.items():
        try:
            future.result()
            created.append(name)
        except Exception as exc:
            error = exc.args[0] if exc.args else None
            if getattr(error, "code", lambda: None)() != ALREADY_EXISTS:
                raise
    return created
