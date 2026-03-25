ALTER TABLE problems ADD COLUMN resume_anchor_lemma_id TEXT;
ALTER TABLE problems ADD COLUMN resume_anchor_owner_decomposition_id TEXT;

ALTER TABLE lemmas ADD COLUMN decomposition_no_accept_rounds INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_problems_resume_anchor_lemma ON problems(resume_anchor_lemma_id);
