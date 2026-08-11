# RTE fixtures

Real responses from `digital.iservices.rte-france.com`, recorded 2026-08-11.
Nothing here is generated. No credentials appear in these files.

Source: RTE Data Portal, Actual Generation v1.1. Check their reuse terms before
this repository goes public.

Both files cover **26 October 2025**, the autumn clock change, requested as the
Paris local day. That is the same window as
`tests/fixtures/entsoe/a75_fr_20251026_dst.xml`, on purpose: two independent
sources describing the same 25 hours of the same grid.

| File | Resource | Shape |
|---|---|---|
| `actual_generation_fr_20251026_dst.json` | `actual_generations_per_production_type` | 13 entries, hourly, 24 values each |
| `generation_mix_15min_fr_20251026_dst.json` | `generation_mix_15min_time_scale` | 22 entries by type and subtype, 96 values each |

## RTE drops the repeated hour, ENTSO-E does not

This is the headline. The Paris day on 26 October 2025 is 25 hours. ENTSO-E
returns 100 quarter-hour positions covering all of it. RTE returns 24 hourly
values covering a 25 hour span, with a two hour jump in the middle:

    2025-10-26T01:00:00+02:00   utc 23:00
    2025-10-26T02:00:00+01:00   utc 01:00     <- 00:00 utc is absent

`02:00` local happens twice that morning, once at UTC+2 and once at UTC+1. RTE
publishes only the second one. An hour of French generation is simply not in
the feed. The 15 minute resource does the same thing: 96 values across a span
that needs 100.

Nothing in the response marks this as a gap. The values either side are
adjacent in the list, so anything that reads the array in order and assumes
hourly steps will be silently wrong for the rest of the day.

This is the single best argument in the project for reconciling two sources
rather than trusting one.

## Other differences from ENTSO-E worth knowing

**Timestamps carry the real local offset**, `+02:00` before the change and
`+01:00` after, so unlike ENTSO-E's position numbering the timestamps are
self-describing and never ambiguous. Where RTE publishes an hour at all, you
know exactly which one it is.

**Pumped storage is one signed series, not two.** ENTSO-E emits separate
generation and consumption series for `B10`. RTE emits a single
`HYDRO_PUMPED_STORAGE` with negative values while pumping: 17 of the 24 hours
on this day are negative, the lowest at -2788 MW. Normalising to the ENTSO-E
convention means splitting the sign into a direction.

**There is a `TOTAL` entry mixed in with the components.** Summing everything
double counts the day. On this fixture the components do sum to `TOTAL`
exactly, in all 24 hours, which makes it a free arithmetic check rather than
just a hazard.

**Values are integers.** ENTSO-E sends up to two decimal places for the same
quantities.

**Production types are names, not codes.** `FOSSIL_HARD_COAL` where ENTSO-E
says `B05`. There is no equivalent of `B25` energy storage. The 15 minute
resource adds a `production_subtype`, so `BIOENERGY` splits into `BIOGAS` and
others.

## `updated_date` is a revision timestamp from the source

Each value can carry `updated_date`, and on this fixture there are four
distinct ones:

    2025-10-25T23:36:01+02:00     12 values
    2025-10-25T23:36:57+02:00     12 values
    2025-10-28T01:11:27+01:00    264 values
    (absent)                      24 values

Twenty-four values were published in near-real-time the evening before, and 264
were revised two days later. The absent ones are the `TOTAL` entry, which is
derived and has no update of its own.

That is the revision behaviour this whole project exists to model, visible in a
single response. It is the source telling us when it changed its mind, which is
not the same as our `known_at` and deserves its own column.
