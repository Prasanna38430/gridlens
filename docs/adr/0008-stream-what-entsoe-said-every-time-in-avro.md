# ADR-0008: stream what ENTSO-E said, every time, in Avro

Status: accepted, 2026-10-05

## Context

Week four adds a streaming path: Redpanda, a producer, and on day 23 a Spark
consumer. Day 24 compares what the stream said with what the batch settled on.
Before writing the producer I had to decide what goes into the stream, how it
is laid out in topics, and how it is encoded. Each of those is hard to change
once a consumer depends on it.

The stream needs a real source. Replaying bronze into a topic would be a batch
wearing a costume. ENTSO-E turned out to be the answer: it publishes the
current day as it goes. At 07:27 UTC on 2026-10-05 it already had 36 periods of
that day, up to 06:45, so it runs about forty minutes behind real time. The
daily Lambda only ever saw a day the morning after.

## Decisions

**The producer polls today's settlement day every fifteen minutes**, and
yesterday's too until 03:00 Paris, since the last periods of a day are
published after midnight. It runs in a small container next to Redpanda and
reads the ENTSO-E token from SSM, where the Lambdas read it.

**Every poll publishes everything it fetched.** Unchanged values included. The
topic is a record of what ENTSO-E said and when, so a value repeated at 10:15 is
a fact just as much as one that changed. `known_at` is the fetch time to the
second, so repeats are told apart from revisions by the quantity, not by the
stamp. Collapsing repeats belongs to the consumer on day 23. Two polls 43
seconds apart produced 1,108 observations of 554 series periods, every second
one a repeat, which is the flood that consumer has to handle.

**One topic, `gridlens.entsoe.generation.v1`.** Project, source, entity,
version. A breaking change gets a `v2` topic and a period where both exist,
not a surprise inside `v1`.

**Keyed by series**: source, zone, production type, direction. Kafka only
orders messages within a partition, and a later revision has to arrive after
the value it revises. Keying by series puts every observation of a series in
one partition, in order. Keying by series and period would also keep each
period in order and spread better across partitions, so I weighed it. I kept
the series, because a gap or a completeness check is a question about one series
over a day, and that becomes a question about one partition. The cost is skew,
measured on the first poll at 231, 203 and 120 messages across the three
partitions.

**Three partitions.** Fifteen series and one core. Enough for the consumer to
read with more than one task, not so many that the broker spends its time on
partitions nobody needs.

**Delete after fourteen days, never compact.** Compaction keeps the newest
value per key and throws the rest away, which in this project means throwing
away the revisions, the one thing it exists to keep. Fourteen days because the
laptop is not always on, and a consumer that was off for a week has to be able
to catch up from the topic.

**Refusals go to `gridlens.entsoe.generation.quarantine.v1`**, as JSON, kept
for thirty days. Same rule as the quarantine bucket in ADR-0003: never dropped.
JSON because a refused row is one that does not fit the schema.

**Avro, with the schema in Redpanda's schema registry.** Each message is one
zero byte, the schema id as four bytes, and the Avro body. I wrote those five
bytes out by hand rather than pull in a registry client library, because it is
five bytes and the library would have been most of the image. The subject is
set to `BACKWARD` compatibility, so a schema a current consumer could not read
is refused by the registry before any producer can use it. Checked against the
running registry: a new optional field with a default is accepted, a new
required field is refused, and `quantity_mw` changed to a double is refused.

**`quantity_mw` is an Avro decimal, `(12, 3)`, not a double.** ADR-0006 took
floating point out of gold. Putting it back into the stream that day 24 compares
with gold would have undone that. Timestamps are `timestamp-micros` in UTC.

**The producer is idempotent with `acks=all`.** A retried send cannot land
twice. Duplicates in the topic should only ever be ENTSO-E repeating itself,
never the transport, so the consumer can treat every duplicate the same way.

## Options I turned down

**JSON values.** No schema to enforce, and the Spark consumer needs a schema
anyway, so the decision would only move somewhere less visible.

**Protobuf.** It would have worked. Avro is what Spark reads with its own
`from_avro`, and it is what the schema registry was built around.

**Publishing only what changed.** Fewer messages, but the producer would need
state that survives restarts, and the consumer's deduplication would have
nothing to prove itself on.

**RTE as the streaming source.** It is the second source this project is
meant to reconcile, and it is still not wired up. But its resource here is
hourly where ENTSO-E is quarter hourly, so comparing the two needs more work
first. It can be a second producer later.

## Consequences

The stream only exists while the laptop and the stream profile are up. A hole
in the topic is expected, and the batch path is still the source of truth.

Partitions and retention are set when a topic is created, and the producer
never changes an existing one. Changing them is a deliberate act.

The consumer has to strip the five byte header before Spark's `from_avro`,
which expects a bare Avro body.

Measured on 2026-10-05 after the first polls: Redpanda 236 MiB and the producer
52 MiB. Redpanda can grow to the 512 MB it is given, and with Airflow at its own
peak that comes within a few dozen MB of the 2 GB WSL cap, so the two profiles
run one at a time.
