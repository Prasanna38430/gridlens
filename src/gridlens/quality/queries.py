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

# One row per series per settlement day. The grain matters: a Paris day runs
# 22:00Z to 22:00Z, so grouping on the UTC date splits every fetch across two
# dates and reports 8 periods on one and 88 on the other. Completeness belongs
# on the day we actually asked for.
#
# The newest day is excluded because it is always in flight. Everything older
# is fair game, and a short day there is a real finding rather than a timing
# artifact.
COMPLETENESS = """
WITH local AS (
    SELECT
        zone,
        CAST(valid_time AT TIME ZONE 'Europe/Paris' AS date) AS settlement_day,
        production_type,
        direction,
        valid_time,
        known_at,
        quantity_mw
    FROM {database}.generation
),
newest AS (
    SELECT max(settlement_day) AS latest FROM local
)
SELECT
    zone,
    settlement_day,
    production_type,
    direction,
    count(DISTINCT valid_time) AS periods,
    count(DISTINCT known_at) AS versions,
    CAST(max(quantity_mw) AS double) AS max_mw
FROM local, newest
WHERE settlement_day < newest.latest
GROUP BY 1, 2, 3, 4
"""
