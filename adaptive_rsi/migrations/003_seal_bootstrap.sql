-- Additive maintenance migration. The database compatibility format remains v2.
-- Entries 1 and 2 retain their legacy identifier hashes; subsequent entries hash SQL bytes.
DROP INDEX IF EXISTS request_log_created;
