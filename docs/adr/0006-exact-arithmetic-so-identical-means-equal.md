# ADR-0006: gold is exact, so identical means equal

Status: accepted, 2026-09-30

## Context

The restatement audit has one job: rebuild last month from bronze and show it
comes out the same as what production holds. Before writing the comparison I
had to decide what "the same" means. The first run decided it for me.

On 2026-09-30 I built August twice from identical input, same bronze rows and
same code, a few minutes apart, and compared the two. 299 of the 364 rows in
`daily_generation_mix` were different.

The cause was one literal. Daily energy was

    sum(net_mw * resolution_minutes / 60.0)

and in Athena `60.0` is a double, not a decimal. That turned the whole sum into
floating point. Floating point addition is not associative, and Athena adds
partial sums on several workers and combines them in whatever order they
finish. Same rows, different order, different last digits. Nuclear on
2026-08-28 was exactly 861,845.2275 MWh. Production said `861845.2274999998`.

The errors were tiny. When I compared the old gold with exact values later, 364
of 494 rows were off, by 5e-10 MWh at most and 2e-15 at least. Nobody will
ever dispute an invoice over a millionth of a kWh. That is what made this a
decision rather than an obvious fix.

## Options

**Compare with a tolerance.** Say 1e-6 MWh. Cheapest, and both singular tests
already did it. But a tolerance is a number I would have to choose and then
defend, and the audit would stop proving "the same" and start proving "close".
For a project whose argument is that a figure can be reproduced exactly, that
is giving away the argument.

**Round before comparing.** `round(x, 6)` on both sides. It works almost always.
Two sums that land either side of a rounding boundary still disagree, and
"almost always" is not something an audit gets to claim.

**Make the arithmetic exact.** Sum in decimals and divide once. Then two builds
have to agree to the last digit, and the comparison is plain equality.

## Decision

Exact arithmetic. `energy_mwh` and `gross_mwh` are `decimal(38,6)`:

    cast(sum(net_mw * resolution_minutes) as decimal(38, 6)) / 60

The sum is taken in MW minutes, which is exact at the source's three decimal
places, and divided by 60 once.

I also tried dividing each row by 60 at scale three before summing. That is
deterministic too, but it rounds 96 times a day, and it came out 0.02 MWh
wrong per day against the exact figure. Reproducibly wrong is not an
improvement.

`share_of_gross` stays a double, on purpose. One division of two exact decimals
gives the same double every time, so it is deterministic. A decimal quotient
would take the scale of its inputs and round every share to 1e-6. So the rule
is not "no doubles in gold". It is "no floating point inside an aggregate".

The tests follow from that. `daily_energy_matches_period_energy` went from a
1e-6 tolerance to exact equality. `shares_sum_to_one_per_day` keeps 1e-9,
because a dozen correctly rounded doubles do not have to add up to exactly 1,
and that is ordinary arithmetic rather than anything nondeterministic.

## Consequences

The audit is a plain `EXCEPT` in both directions plus row counts. Any
difference at all means the data, the code or the bound changed, and the
manifest says which commit and snapshot production used. September matched
with zero differences on 2026-10-01 and again on 2026-10-05.

`energy_mwh` changed type, double to `decimal(38,6)`. Nothing reads gold yet,
but the serving endpoint will, and so will any other engine I point at these
tables later. They all have to read decimals properly.

Exactness depends on the resolution. At 15 minutes,
`sum(net_mw * 15) / 60` is `sum(net_mw) / 4`, which needs five decimal places,
so scale six loses nothing. The same holds for any resolution that is a
multiple of three minutes. A source at some other resolution would make the
final division round. Still deterministic, no longer exact.

A passing audit proves reproducibility, not correctness. 2026-09-18 was an
hour short for two weeks and the audit passed every day it ran, rightly,
because production and the rebuild were short in exactly the same way.

Every new gold model can fall into the same trap. The question to ask in review
is whether there is a double anywhere inside a `sum`.
