# ADR-0003: validate at the edge, quarantine rather than drop

Status: accepted
Date: 2026-08-11

## Context

Three days of reading real ENTSO-E responses turned up problems that a schema
alone does not catch, and one that nothing downstream can catch at all.

A mistyped bidding zone is invisible after the call. I sent
`in_Domain=10YNOTAREALZONE1` and got back exactly what an empty window returns:
http 200, an acknowledgement, reason code 999, "No matching data found". There
is no error. A backfill pointed at a typo reports a quiet source for the whole
period and looks like a data availability problem rather than a bug in my
config. The only place to catch that is before the request goes out.

Reason code 999 carries no information either. The same code comes back for
authentication failure and for an empty window. Branching on it would be
branching on nothing.

Then there is the shape of the data. Point positions are sparse, so a series
can be 92 points spread over 100 positions. Pumped storage and batteries appear
twice in one document, once generating and once consuming, and summing by
production type without reading the direction double counts them. Quantities
arrive with up to two decimal places and the largest I have seen is 39,733 MW.
None of that is documented anywhere I could find. All of it was learned by
looking.

The last piece is what to do with a row that fails. The instinct is to drop it
and log a count. That is the wrong instinct for a project whose whole claim is
that history is reproducible. A dropped row leaves a hole that looks identical
to a period the source never published, and the evidence of what actually
arrived is gone.

## Decision

A gate sits between parsing and bronze. It splits a parsed document three ways.

**Records** that satisfy the contract. Zone must be a known bidding zone, from
an enum built by reading the platform's own area list rather than by typing
codes I remembered. Production type must be a known code. Timestamps must be
UTC-aware and land on a resolution boundary. Quantity must be non-negative, at
most three decimal places, and below 150,000 MW, which is a unit-error detector
rather than a claim about the grid: a kilowatt figure mislabelled as megawatts
lands a thousand times over it. `known_at` must not precede `valid_time`,
because a realised value known before its period has finished is either a clock
problem here or a forecast wearing the wrong label. Unknown fields are refused
outright, so a new element upstream stops the pipeline instead of being
silently ignored.

**Violations**, written to quarantine with the reason, the failing detail, and
the payload as it was, every field as text. They are replayable. Nothing is
dropped.

**Gaps**, which are a third thing and not a failure. A position the source did
not publish is absence, and it gets recorded with its real timestamp rather
than being inferred later from a row count. The gate does not decide whether a
gap means zero, unknown, or an outage. Deciding that here would bake the guess
into bronze permanently, and bronze is append-only.

The gate never raises on bad data. It raises only on programmer error.

`known_at` comes from the caller, not from the document. The platform's
`createdDateTime` is when it rendered the response: requesting October 2025
today stamps it with today. It is provenance, never a clock.

## Consequences

Adding a bidding zone is now a code change and a pull request. That is slower
than a config string and it is the point. The alternative is a typo that
produces a plausible empty result.

Quarantine grows. Every bad row is kept forever, partitioned by the date we saw
it, and nothing prunes it. At the volumes here that is negligible, but it is a
cost that only goes one way and it needs a lifecycle rule eventually. I have
not written one, because I would rather find out what actually accumulates
first.

Refusing unknown fields means an upstream addition breaks ingestion rather than
being ignored. I think that is right for settlement data and I accept that it
will page me for something harmless one day.

The three quantity bounds are guesses calibrated on two days of French data.
The 150,000 MW ceiling is defensible today and will need revisiting if this
ever covers a zone with more capacity than France. When it fires on something
real, the row is in quarantine rather than lost, which is exactly the property
this decision exists to buy.

The sink is a file on disk right now. The S3 implementation lands when the
bucket does. The partition layout is the same string in both, so a quarantined
row read back in a test has the same shape as one read back from the lake.
