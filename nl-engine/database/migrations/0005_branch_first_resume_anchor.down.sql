DROP INDEX IF EXISTS idx_problems_resume_anchor_lemma;

ALTER TABLE lemmas DROP COLUMN IF EXISTS decomposition_no_accept_rounds;

ALTER TABLE problems DROP COLUMN IF EXISTS resume_anchor_owner_decomposition_id;
ALTER TABLE problems DROP COLUMN IF EXISTS resume_anchor_lemma_id;
