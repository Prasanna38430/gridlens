from __future__ import annotations

# The contract gate already checks every row as it arrives. These are the
# things one batch cannot see: whether the table as a whole still holds the
# invariants the writer is supposed to guarantee.

# One row, five numbers, all of which should be zero or small.
INVARIANTS = """
WITH keyed AS (
    SELECT source, zone, production_type, direction, valid_time, known_at,
           count(*) AS copies
    FROM {database}.generation
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT
    (SELECT coalesce(sum(copies - 1), 0) FROM keyed WHERE copies > 1)
        AS duplicate_keys,
    (SELECT count(*) FROM {database}.generation
     WHERE valid_time > current_timestamp) AS future_periods,
    (SELECT count(*) FROM {database}.generation
     WHERE known_at < valid_time) AS known_before_it_happened,
    (SELECT count(*) FROM {database}.generation
     WHERE quantity_mw < 0) AS negative_quantities,
    (SELECT date_diff('hour', max(known_at), current_timestamp)
     FROM {database}.generation) AS hours_since_last_learned
"""

# One row per settlement day, not per series per day.
#
# The grain is the point. A series is allowed to be sparse, because entso-e
# omits positions it has no value for rather than sending zeros, and solar does
# that every night. Its count tracks daylight: a flat 70 periods through August
# and 62 by September. Any fixed floor per series is a number measured in one
# season and wrong in the next, and a floor low enough for winter solar would
# let nuclear drop from 96 to 40 unnoticed.
#
# A short day is a different thing. When a fetch lands before the source has
# finished publishing, every series is short and the day as a whole is missing
# periods. So the question asked here is whether the day, across all of its
# series, has every period the calendar says it has. That is a property of the
# calendar rather than the weather, and it needs no recalibration.
#
# Grouped on the Paris date because a settlement day runs 22:00Z to 22:00Z, so
# the UTC date splits every fetch across two dates.
#
# The newest day is excluded because it is always in flight.
COMPLETENESS = """
WITH local AS (
    SELECT
        zone,
        CAST(valid_time AT TIME ZONE 'Europe/Paris' AS date) AS settlement_day,
        production_type,
        direction,
        valid_time,
        quantity_mw
    FROM {database}.generation
),
newest AS (
    SELECT max(settlement_day) AS latest FROM local
),
per_series AS (
    SELECT zone, settlement_day, production_type, direction,
           count(DISTINCT valid_time) AS periods
    FROM local
    GROUP BY 1, 2, 3, 4
),
per_day AS (
    SELECT
        zone,
        settlement_day,
        count(DISTINCT valid_time) AS periods,
        count(DISTINCT production_type || '/' || direction) AS series,
        CAST(max(quantity_mw) AS double) AS max_mw
    FROM local
    GROUP BY 1, 2
),
densest AS (
    SELECT zone, settlement_day, max(periods) AS densest_series
    FROM per_series
    GROUP BY 1, 2
)
SELECT
    p.zone,
    p.settlement_day,
    p.periods,
    p.series,
    d.densest_series,
    p.max_mw
FROM per_day p
JOIN densest d
    ON d.zone = p.zone
   AND d.settlement_day = p.settlement_day
CROSS JOIN newest
WHERE p.settlement_day < newest.latest
"""
