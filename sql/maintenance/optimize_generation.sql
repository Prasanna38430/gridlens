-- Bin-pack the partitions recent writes touched. Bounded rather than a full
-- table rewrite, and the reasoning is in sql/README.md.

OPTIMIZE gridlens_bronze.generation REWRITE DATA USING BIN_PACK
WHERE valid_time >= current_date - INTERVAL '7' DAY
