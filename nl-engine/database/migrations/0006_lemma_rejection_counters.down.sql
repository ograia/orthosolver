ALTER TABLE lemmas ADD COLUMN decomposition_no_accept_rounds INTEGER NOT NULL DEFAULT 0;

ALTER TABLE lemmas DROP COLUMN IF EXISTS minor_rejection_count;
ALTER TABLE lemmas DROP COLUMN IF EXISTS consecutive_fatal_rejections;
