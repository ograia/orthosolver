ALTER TABLE lemmas ADD COLUMN consecutive_fatal_rejections INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lemmas ADD COLUMN minor_rejection_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE lemmas DROP COLUMN IF EXISTS decomposition_no_accept_rounds;
