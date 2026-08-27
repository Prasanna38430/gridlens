-- Drop snapshots past the retention set in retention_generation.sql and delete
-- the files they were the last to reference. This is the step that reclaims
-- what compaction leaves behind, and it is not reversible.

VACUUM gridlens_bronze.generation
