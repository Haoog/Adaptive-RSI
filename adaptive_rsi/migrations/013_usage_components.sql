-- NULL preserves the billing interpretation of calls recorded before this migration.
ALTER TABLE calls ADD COLUMN usage_components_known INTEGER CHECK (usage_components_known IN (0, 1));
