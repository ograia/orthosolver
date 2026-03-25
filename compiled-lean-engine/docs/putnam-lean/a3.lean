import Mathlib

open scoped BigOperators

/- ===== Statements ===== -/
/-- Adjacency in H_n: two vertices differ by ±1 in exactly one coordinate. -/
def HnAdj {n : ℕ} (v w : Fin n → Fin 3) : Prop :=
  ∃ j : Fin n, ((v j : ℕ) + 1 = (w j : ℕ) ∨ (w j : ℕ) + 1 = (v j : ℕ)) ∧
    ∀ k : Fin n, k ≠ j → v k = w k

/-- The zero vertex (0,...,0) in {0,1,2}^n. -/
def zeroVtx (n : ℕ) : Fin n → Fin 3 := fun _ => 0

/-- Bob has a winning strategy in the no-repetition alternating game on H_n
    starting at (0,...,0). Alice moves first. A move goes to an adjacent
    unvisited vertex. A player with no legal move loses. -/
def BobWinsGame (n : ℕ) : Prop :=
  ∃ σ : List (Fin n → Fin 3) → (Fin n → Fin 3),
    ∀ (play : List (Fin n → Fin 3))
      (hlen : play.length ≥ 1)
      (hstart : play.get ⟨0, by omega⟩ = zeroVtx n)
      (hnodup : play.Nodup)
      (hadj : ∀ (i : ℕ) (hi : i + 1 < play.length),
        HnAdj (play.get ⟨i, by omega⟩) (play.get ⟨i + 1, hi⟩))
      (hmaximal : ∀ w, HnAdj (play.get ⟨play.length - 1, by omega⟩) w → w ∈ play)
      (hbob : ∀ (j : ℕ) (hj : 2 * j + 2 < play.length),
        play.get ⟨2 * j + 2, by omega⟩ = σ (play.take (2 * j + 2))),
    (play.length - 1) % 2 = 0

-- L1: Base case — H_1 admits a Hamiltonian path starting at (0).
-- L2: Prepending a fixed digit to each vertex of a path in H_k yields
-- a path (and its reverse) in H_{k+1}.
-- L3: Zigzag construction — given a Hamiltonian path Q in H_k, there exists
-- a Hamiltonian path R in H_{k+1} whose first vertex is (0, Q(0)).
-- L4: For every n ≥ 1, H_n has a Hamiltonian path starting at (0,...,0).
-- L5: For any path in H_n starting at (0,...,0), the parity of the coordinate
-- sum of the i-th vertex equals i mod 2.
-- L6: Given a Hamiltonian path with the parity property, Bob has a winning
-- strategy (follow the path successor after each of Alice's moves).
-- Root: For every n ≥ 1, Bob (the second player) has a winning strategy in the
-- no-repetition game on {0,1,2}^n starting at (0,...,0).

/- ===== Lemmas ===== -/

theorem lem_20260315232011_3d908a3a : ∃ P : Fin (3 ^ 1) → (Fin 1 → Fin 3), Function.Bijective P ∧ P ⟨0, by positivity⟩ = zeroVtx 1 ∧ ∀ (i : ℕ) (hi : i + 1 < 3 ^ 1), HnAdj (P ⟨i, by omega⟩) (P ⟨i + 1, hi⟩) := by
  -- P maps index i to the constant 1-tuple with value i
  refine ⟨fun i => fun _ => ⟨i.val, by omega⟩, ?_, ?_, ?_⟩
  · -- Bijective
    constructor
    · -- Injective
      intro a b hab
      have := congr_fun hab ⟨0, by omega⟩
      simp at this
      exact this
    · -- Surjective
      intro f
      refine ⟨⟨(f ⟨0, by omega⟩).val, by omega⟩, ?_⟩
      ext j
      fin_cases j
      simp
  · -- P ⟨0, _⟩ = zeroVtx 1
    ext j
    fin_cases j
    simp [zeroVtx]
  · -- Adjacency: consecutive vertices differ by 1 in the unique coordinate
    intro i hi
    refine ⟨⟨0, by omega⟩, ?_, ?_⟩
    · left; simp
    · intro k hk
      fin_cases k
      exact absurd rfl hk

theorem lem_20260315232044_137cba8d (k : ℕ) (hk : k ≥ 1) (m : ℕ) (Q : Fin m → (Fin k → Fin 3)) (hpath : ∀ (i : ℕ) (hi : i + 1 < m), HnAdj (Q ⟨i, by omega⟩) (Q ⟨i + 1, hi⟩)) (a : Fin 3) : (∀ (i : ℕ) (hi : i + 1 < m), HnAdj (Fin.cons a (Q ⟨i, by omega⟩)) (Fin.cons a (Q ⟨i + 1, hi⟩))) ∧ (∀ (i : ℕ) (hi : i + 1 < m), HnAdj (Fin.cons a (Q ⟨m - 1 - i, by omega⟩)) (Fin.cons a (Q ⟨m - 2 - i, by omega⟩))) := by
  -- Helper: prepending a fixed digit preserves adjacency
  have cons_adj : ∀ (v w : Fin k → Fin 3), HnAdj v w →
      HnAdj (Fin.cons a v) (Fin.cons a w) := by
    intro v w ⟨j, hj1, hj2⟩
    refine ⟨Fin.succ j, ?_, ?_⟩
    · simp only [Fin.cons_succ]; exact hj1
    · intro l hl
      have lcases : l = 0 ∨ ∃ l' : Fin k, l = Fin.succ l' := by
        rcases l with ⟨_ | l', hl'⟩
        · exact Or.inl (Fin.ext rfl)
        · exact Or.inr ⟨⟨l', by omega⟩, Fin.ext rfl⟩
      obtain rfl | ⟨l', rfl⟩ := lcases
      · simp [Fin.cons_zero]
      · simp only [Fin.cons_succ]
        exact hj2 l' (fun h => hl (congr_arg Fin.succ h))
  -- Helper: HnAdj is symmetric
  have hnadj_symm : ∀ {n : ℕ} (v w : Fin n → Fin 3), HnAdj v w → HnAdj w v := by
    intro n v w ⟨j, hj1, hj2⟩
    exact ⟨j, by rcases hj1 with h | h <;> [exact Or.inr h; exact Or.inl h],
      fun l hl => (hj2 l hl).symm⟩
  constructor
  · -- Forward path
    intro i hi; exact cons_adj _ _ (hpath i hi)
  · -- Reverse path
    intro i hi
    have h2 : (m - 2 - i) + 1 < m := by omega
    have hstep := hpath (m - 2 - i) h2
    have heq : (⟨(m - 2 - i) + 1, h2⟩ : Fin m) = ⟨m - 1 - i, by omega⟩ := by
      apply Fin.ext; show (m - 2 - i) + 1 = m - 1 - i; omega
    rw [show Q ⟨(m - 2 - i) + 1, h2⟩ = Q ⟨m - 1 - i, by omega⟩ from congr_arg Q heq] at hstep
    exact cons_adj _ _ (hnadj_symm _ _ hstep)

theorem lem_20260315232127_37cf2c0e (k : ℕ) (hk : k ≥ 1) (Q : Fin (3 ^ k) → (Fin k → Fin 3)) (hbij : Function.Bijective Q) (hadj : ∀ (i : ℕ) (hi : i + 1 < 3 ^ k), HnAdj (Q ⟨i, by omega⟩) (Q ⟨i + 1, hi⟩)) : ∃ R : Fin (3 ^ (k + 1)) → (Fin (k + 1) → Fin 3), Function.Bijective R ∧ (∀ (i : ℕ) (hi : i + 1 < 3 ^ (k + 1)), HnAdj (R ⟨i, by omega⟩) (R ⟨i + 1, hi⟩)) ∧ R ⟨0, by positivity⟩ = Fin.cons (0 : Fin 3) (Q ⟨0, by positivity⟩) := by
  have hn_pos : 3 ^ k ≥ 1 := Nat.one_le_pow k 3 (by omega)
  let R : Fin (3 ^ (k + 1)) → (Fin (k + 1) → Fin 3) := fun ⟨i, hi⟩ =>
    if h : i < 3 ^ k then
      Fin.cons (0 : Fin 3) (Q ⟨i, h⟩)
    else if h2 : i < 2 * 3 ^ k then
      Fin.cons (1 : Fin 3) (Q ⟨2 * 3 ^ k - 1 - i, by omega⟩)
    else
      Fin.cons (2 : Fin 3) (Q ⟨i - 2 * 3 ^ k, by omega⟩)
  -- Helper: Fin.cons with same head preserves HnAdj
  have hcons_tail_adj : ∀ (a : Fin 3) (u v : Fin k → Fin 3),
      HnAdj u v → HnAdj (Fin.cons a u) (Fin.cons a v) := by
    intro a u v ⟨j, hdiff, hsame⟩
    refine ⟨Fin.succ j, ?_, ?_⟩
    · simp only [Fin.cons_succ]; exact hdiff
    · intro m hm
      revert hm
      refine Fin.cases (fun hm => ?_) (fun m' hm => ?_) m
      · simp [Fin.cons_zero]
      · simp only [Fin.cons_succ]
        exact hsame m' (fun h => hm (congr_arg Fin.succ h))
  -- Helper: Fin.cons with same tail, heads differ by ±1
  have hcons_head_adj : ∀ (a b : Fin 3) (u : Fin k → Fin 3),
      ((a : ℕ) + 1 = (b : ℕ) ∨ (b : ℕ) + 1 = (a : ℕ)) →
      HnAdj (Fin.cons a u) (Fin.cons b u) := by
    intro a b u hdiff
    refine ⟨0, by simp only [Fin.cons_zero]; exact hdiff, fun m hm => ?_⟩
    revert hm
    refine Fin.cases (fun hm => absurd rfl hm) (fun m' _ => ?_) m
    simp [Fin.cons_succ]
  -- Helper: HnAdj is symmetric
  have hnadj_symm : ∀ {n : ℕ} {v w : Fin n → Fin 3}, HnAdj v w → HnAdj w v := by
    intro n v w ⟨j, hdiff, hsame⟩
    exact ⟨j, hdiff.symm, fun m hm => (hsame m hm).symm⟩
  refine ⟨R, ?_, ?_, ?_⟩
  · -- BIJECTIVITY
    constructor
    · -- Injectivity
      intro ⟨a, ha⟩ ⟨b, hb⟩ hab
      simp only [Fin.mk.injEq]
      by_cases ha0 : a < 3 ^ k
      · have hRa : R ⟨a, ha⟩ = Fin.cons 0 (Q ⟨a, ha0⟩) := by simp only [R]; exact dif_pos ha0
        by_cases hb0 : b < 3 ^ k
        · have hRb : R ⟨b, hb⟩ = Fin.cons 0 (Q ⟨b, hb0⟩) := by simp only [R]; exact dif_pos hb0
          rw [hRa, hRb] at hab
          have htail : Q ⟨a, ha0⟩ = Q ⟨b, hb0⟩ := by
            funext i; have := congr_fun hab (Fin.succ i); simp only [Fin.cons_succ] at this; exact this
          exact congr_arg Fin.val (hbij.1 htail)
        · exfalso
          by_cases hb1 : b < 2 * 3 ^ k
          · have hRb : R ⟨b, hb⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - b, by omega⟩) := by
              simp only [R]; rw [dif_neg hb0, dif_pos hb1]
            rw [hRa, hRb] at hab
            have := congr_fun hab 0; simp only [Fin.cons_zero] at this; exact absurd this (by decide)
          · have hRb : R ⟨b, hb⟩ = Fin.cons 2 (Q ⟨b - 2 * 3 ^ k, by omega⟩) := by
              simp only [R]; rw [dif_neg hb0, dif_neg hb1]
            rw [hRa, hRb] at hab
            have := congr_fun hab 0; simp only [Fin.cons_zero] at this; exact absurd this (by decide)
      · by_cases ha1 : a < 2 * 3 ^ k
        · have hRa : R ⟨a, ha⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - a, by omega⟩) := by
            simp only [R]; rw [dif_neg ha0, dif_pos ha1]
          by_cases hb0 : b < 3 ^ k
          · exfalso
            have hRb : R ⟨b, hb⟩ = Fin.cons 0 (Q ⟨b, hb0⟩) := by simp only [R]; exact dif_pos hb0
            rw [hRa, hRb] at hab
            have := congr_fun hab 0; simp only [Fin.cons_zero] at this; exact absurd this (by decide)
          · by_cases hb1 : b < 2 * 3 ^ k
            · have hRb : R ⟨b, hb⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - b, by omega⟩) := by
                simp only [R]; rw [dif_neg hb0, dif_pos hb1]
              rw [hRa, hRb] at hab
              have htail : Q ⟨2 * 3 ^ k - 1 - a, by omega⟩ = Q ⟨2 * 3 ^ k - 1 - b, by omega⟩ := by
                funext i; have := congr_fun hab (Fin.succ i); simp only [Fin.cons_succ] at this; exact this
              have := congr_arg Fin.val (hbij.1 htail)
              simp only [Fin.val_mk] at this
              omega
            · exfalso
              have hRb : R ⟨b, hb⟩ = Fin.cons 2 (Q ⟨b - 2 * 3 ^ k, by omega⟩) := by
                simp only [R]; rw [dif_neg hb0, dif_neg hb1]
              rw [hRa, hRb] at hab
              have := congr_fun hab 0; simp only [Fin.cons_zero] at this; exact absurd this (by decide)
        · have hRa : R ⟨a, ha⟩ = Fin.cons 2 (Q ⟨a - 2 * 3 ^ k, by omega⟩) := by
            simp only [R]; rw [dif_neg ha0, dif_neg ha1]
          by_cases hb0 : b < 3 ^ k
          · exfalso
            have hRb : R ⟨b, hb⟩ = Fin.cons 0 (Q ⟨b, hb0⟩) := by simp only [R]; exact dif_pos hb0
            rw [hRa, hRb] at hab
            have := congr_fun hab 0; simp only [Fin.cons_zero] at this; exact absurd this (by decide)
          · by_cases hb1 : b < 2 * 3 ^ k
            · exfalso
              have hRb : R ⟨b, hb⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - b, by omega⟩) := by
                simp only [R]; rw [dif_neg hb0, dif_pos hb1]
              rw [hRa, hRb] at hab
              have := congr_fun hab 0; simp only [Fin.cons_zero] at this; exact absurd this (by decide)
            · have hRb : R ⟨b, hb⟩ = Fin.cons 2 (Q ⟨b - 2 * 3 ^ k, by omega⟩) := by
                simp only [R]; rw [dif_neg hb0, dif_neg hb1]
              rw [hRa, hRb] at hab
              have htail : Q ⟨a - 2 * 3 ^ k, by omega⟩ = Q ⟨b - 2 * 3 ^ k, by omega⟩ := by
                funext i; have := congr_fun hab (Fin.succ i); simp only [Fin.cons_succ] at this; exact this
              have := congr_arg Fin.val (hbij.1 htail)
              simp only [Fin.val_mk] at this
              omega
    · -- Surjectivity
      intro v
      obtain ⟨⟨j, hj⟩, hjQ⟩ := hbij.2 (Fin.tail v)
      have hv0_lt : (v 0).val < 3 := (v 0).isLt
      have hcases : (v 0).val = 0 ∨ (v 0).val = 1 ∨ (v 0).val = 2 := by omega
      rcases hcases with hv0 | hv0 | hv0
      · -- v 0 has val 0. Use index j in block 0.
        refine ⟨⟨j, by omega⟩, ?_⟩
        funext m; refine Fin.cases ?_ (fun m' => ?_) m
        · simp only [R, dif_pos hj, Fin.cons_zero]
          exact Fin.ext hv0 |>.symm
        · simp only [R, dif_pos hj, Fin.cons_succ]
          exact congr_fun hjQ m'
      · -- v 0 has val 1. Use index 2·3^k - 1 - j in block 1.
        refine ⟨⟨2 * 3 ^ k - 1 - j, by omega⟩, ?_⟩
        funext m; refine Fin.cases ?_ (fun m' => ?_) m
        · simp only [R, dif_neg (show ¬ 2 * 3 ^ k - 1 - j < 3 ^ k by omega),
            dif_pos (show 2 * 3 ^ k - 1 - j < 2 * 3 ^ k by omega), Fin.cons_zero]
          exact Fin.ext hv0 |>.symm
        · simp only [R, dif_neg (show ¬ 2 * 3 ^ k - 1 - j < 3 ^ k by omega),
            dif_pos (show 2 * 3 ^ k - 1 - j < 2 * 3 ^ k by omega), Fin.cons_succ]
          have hfin : (⟨2 * 3 ^ k - 1 - (2 * 3 ^ k - 1 - j), by omega⟩ : Fin (3 ^ k)) = ⟨j, hj⟩ := by
            simp only [Fin.mk.injEq]; omega
          rw [hfin]
          exact congr_fun hjQ m'
      · -- v 0 has val 2. Use index 2·3^k + j in block 2.
        refine ⟨⟨2 * 3 ^ k + j, by omega⟩, ?_⟩
        funext m; refine Fin.cases ?_ (fun m' => ?_) m
        · simp only [R, dif_neg (show ¬ 2 * 3 ^ k + j < 3 ^ k by omega),
            dif_neg (show ¬ 2 * 3 ^ k + j < 2 * 3 ^ k by omega), Fin.cons_zero]
          exact Fin.ext hv0 |>.symm
        · simp only [R, dif_neg (show ¬ 2 * 3 ^ k + j < 3 ^ k by omega),
            dif_neg (show ¬ 2 * 3 ^ k + j < 2 * 3 ^ k by omega), Fin.cons_succ]
          have hfin : (⟨2 * 3 ^ k + j - 2 * 3 ^ k, by omega⟩ : Fin (3 ^ k)) = ⟨j, hj⟩ := by
            simp only [Fin.mk.injEq]; omega
          rw [hfin]
          exact congr_fun hjQ m'
  · -- ADJACENCY
    intro i hi
    by_cases h0 : i + 1 < 3 ^ k
    · -- Both in block 0
      have hRi : R ⟨i, by omega⟩ = Fin.cons 0 (Q ⟨i, by omega⟩) := by
        simp only [R]; exact dif_pos (by omega : i < 3 ^ k)
      have hRi1 : R ⟨i + 1, hi⟩ = Fin.cons 0 (Q ⟨i + 1, h0⟩) := by
        simp only [R]; exact dif_pos h0
      rw [hRi, hRi1]
      exact hcons_tail_adj 0 _ _ (hadj i h0)
    · by_cases h1 : i + 1 = 3 ^ k
      · -- Junction block 0 → block 1
        have hRi : R ⟨i, by omega⟩ = Fin.cons 0 (Q ⟨i, by omega⟩) := by
          simp only [R]; exact dif_pos (by omega : i < 3 ^ k)
        have hRi1 : R ⟨i + 1, hi⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - (i + 1), by omega⟩) := by
          simp only [R]; rw [dif_neg (by omega : ¬ i + 1 < 3 ^ k), dif_pos (by omega : i + 1 < 2 * 3 ^ k)]
        rw [hRi, hRi1]
        have hfin : (⟨2 * 3 ^ k - 1 - (i + 1), by omega⟩ : Fin (3 ^ k)) = ⟨i, by omega⟩ := by
          simp only [Fin.mk.injEq]; omega
        rw [hfin]
        exact hcons_head_adj 0 1 (Q ⟨i, by omega⟩) (Or.inl (by decide))
      · by_cases h2 : i + 1 < 2 * 3 ^ k
        · -- Both in block 1
          have hRi : R ⟨i, by omega⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - i, by omega⟩) := by
            simp only [R]; rw [dif_neg (by omega : ¬ i < 3 ^ k), dif_pos (by omega : i < 2 * 3 ^ k)]
          have hRi1 : R ⟨i + 1, hi⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - (i + 1), by omega⟩) := by
            simp only [R]; rw [dif_neg (by omega : ¬ i + 1 < 3 ^ k), dif_pos h2]
          rw [hRi, hRi1]
          -- m = 2n-2-i, m+1 = 2n-1-i. hadj m gives HnAdj Q(m) Q(m+1), need reversed.
          have hm_bound : (2 * 3 ^ k - 1 - (i + 1)) + 1 < 3 ^ k := by omega
          have hadj_m := hadj (2 * 3 ^ k - 1 - (i + 1)) hm_bound
          have hfin : (⟨2 * 3 ^ k - 1 - (i + 1) + 1, hm_bound⟩ : Fin (3 ^ k)) =
              ⟨2 * 3 ^ k - 1 - i, by omega⟩ := by
            simp only [Fin.mk.injEq]; omega
          rw [hfin] at hadj_m
          exact hcons_tail_adj 1 _ _ (hnadj_symm hadj_m)
        · by_cases h3 : i + 1 = 2 * 3 ^ k
          · -- Junction block 1 → block 2
            have hRi : R ⟨i, by omega⟩ = Fin.cons 1 (Q ⟨2 * 3 ^ k - 1 - i, by omega⟩) := by
              simp only [R]; rw [dif_neg (by omega : ¬ i < 3 ^ k), dif_pos (by omega : i < 2 * 3 ^ k)]
            have hRi1 : R ⟨i + 1, hi⟩ = Fin.cons 2 (Q ⟨i + 1 - 2 * 3 ^ k, by omega⟩) := by
              simp only [R]; rw [dif_neg (by omega : ¬ i + 1 < 3 ^ k), dif_neg (by omega : ¬ i + 1 < 2 * 3 ^ k)]
            rw [hRi, hRi1]
            have hfin1 : (⟨2 * 3 ^ k - 1 - i, by omega⟩ : Fin (3 ^ k)) = ⟨0, by omega⟩ := by
              simp only [Fin.mk.injEq]; omega
            have hfin2 : (⟨i + 1 - 2 * 3 ^ k, by omega⟩ : Fin (3 ^ k)) = ⟨0, by omega⟩ := by
              simp only [Fin.mk.injEq]; omega
            rw [hfin1, hfin2]
            exact hcons_head_adj 1 2 (Q ⟨0, by omega⟩) (Or.inl (by decide))
          · -- Both in block 2
            have hRi : R ⟨i, by omega⟩ = Fin.cons 2 (Q ⟨i - 2 * 3 ^ k, by omega⟩) := by
              simp only [R]; rw [dif_neg (by omega : ¬ i < 3 ^ k), dif_neg (by omega : ¬ i < 2 * 3 ^ k)]
            have hRi1 : R ⟨i + 1, hi⟩ = Fin.cons 2 (Q ⟨i + 1 - 2 * 3 ^ k, by omega⟩) := by
              simp only [R]; rw [dif_neg (by omega : ¬ i + 1 < 3 ^ k), dif_neg (by omega : ¬ i + 1 < 2 * 3 ^ k)]
            rw [hRi, hRi1]
            have hm_bound : (i - 2 * 3 ^ k) + 1 < 3 ^ k := by omega
            have hadj_m := hadj (i - 2 * 3 ^ k) hm_bound
            have hfin : (⟨i - 2 * 3 ^ k + 1, hm_bound⟩ : Fin (3 ^ k)) =
                ⟨i + 1 - 2 * 3 ^ k, by omega⟩ := by
              simp only [Fin.mk.injEq]; omega
            rw [hfin] at hadj_m
            exact hcons_tail_adj 2 _ _ hadj_m
  · -- R(0) = Fin.cons 0 (Q(0))
    show R ⟨0, by positivity⟩ = _
    simp only [R]
    rw [dif_pos (show 0 < 3 ^ k by omega)]

theorem lem_20260315232216_4cebae53 (n : ℕ) (hn : n ≥ 1) : ∃ P : Fin (3 ^ n) → (Fin n → Fin 3), Function.Bijective P ∧ P ⟨0, by positivity⟩ = zeroVtx n ∧ ∀ (i : ℕ) (hi : i + 1 < 3 ^ n), HnAdj (P ⟨i, by omega⟩) (P ⟨i + 1, hi⟩) := by
  induction n with
  | zero => omega
  | succ k ih =>
    by_cases hk : k = 0
    · subst hk; exact lem_20260315232011_3d908a3a
    · have hk1 : k ≥ 1 := by omega
      obtain ⟨Q, hQbij, hQstart, hQadj⟩ := ih hk1
      obtain ⟨R, hRbij, hRadj, hR0⟩ := lem_20260315232127_37cf2c0e k hk1 Q hQbij hQadj
      refine ⟨R, hRbij, ?_, hRadj⟩
      rw [hR0, hQstart]
      ext i
      refine Fin.cases rfl (fun j => rfl) i

theorem lem_20260315232302_10f794c0 (n : ℕ) (hn : n ≥ 1) (N : ℕ) (hN : N ≥ 1) (P : Fin N → (Fin n → Fin 3)) (hstart : P ⟨0, by omega⟩ = zeroVtx n) (hpath : ∀ (i : ℕ) (hi : i + 1 < N), HnAdj (P ⟨i, by omega⟩) (P ⟨i + 1, hi⟩)) (i : Fin N) : (∑ j : Fin n, (P i j).val) % 2 = (i : ℕ) % 2 := by
  obtain ⟨i, hi⟩ := i
  induction i with
  | zero =>
    simp only [Fin.val_mk, Nat.zero_mod]
    have h0 : P ⟨0, hi⟩ = zeroVtx n := hstart
    simp [h0, zeroVtx]
  | succ k ih =>
    simp only [Fin.val_mk]
    have hk : k < N := by omega
    have ihk := ih hk
    simp only [Fin.val_mk] at ihk
    obtain ⟨t, hdiff, hagree⟩ := hpath k hi
    have hmem : t ∈ (Finset.univ : Finset (Fin n)) := Finset.mem_univ t
    -- Decompose sums: ∑ = f(t) + ∑_{j≠t}
    have d1 : (P ⟨k + 1, hi⟩ t).val + ∑ j ∈ (Finset.univ : Finset (Fin n)).erase t, (P ⟨k + 1, hi⟩ j).val =
              ∑ j : Fin n, (P ⟨k + 1, hi⟩ j).val :=
      Finset.add_sum_erase Finset.univ (fun j => (P ⟨k + 1, hi⟩ j).val) hmem
    have d2 : (P ⟨k, hk⟩ t).val + ∑ j ∈ (Finset.univ : Finset (Fin n)).erase t, (P ⟨k, hk⟩ j).val =
              ∑ j : Fin n, (P ⟨k, hk⟩ j).val :=
      Finset.add_sum_erase Finset.univ (fun j => (P ⟨k, hk⟩ j).val) hmem
    -- Sums over coordinates ≠ t are equal (coordinates agree there)
    have heq : ∑ j ∈ (Finset.univ : Finset (Fin n)).erase t, (P ⟨k + 1, hi⟩ j).val =
               ∑ j ∈ (Finset.univ : Finset (Fin n)).erase t, (P ⟨k, hk⟩ j).val :=
      Finset.sum_congr rfl fun j hj =>
        congr_arg Fin.val (hagree j (Finset.mem_erase.mp hj).1).symm
    -- hdiff gives ±1 change at coordinate t; omega closes both cases
    rcases hdiff with h | h <;> omega

theorem lem_20260315232346_5554102e (n : ℕ) (hn : n ≥ 1) (P : Fin (3 ^ n) → (Fin n → Fin 3)) (hbij : Function.Bijective P) (hstart : P ⟨0, by positivity⟩ = zeroVtx n) (hadj : ∀ (i : ℕ) (hi : i + 1 < 3 ^ n), HnAdj (P ⟨i, by omega⟩) (P ⟨i + 1, hi⟩)) (hparity : ∀ (i : Fin (3 ^ n)), (∑ j : Fin n, (P i j).val) % 2 = (i : ℕ) % 2) : BobWinsGame n := by
  unfold BobWinsGame
  have h3n_pos : 0 < 3 ^ n := by positivity
  let Pequiv := Equiv.ofBijective P hbij
  -- Bob's strategy: look at last vertex, follow P-successor (mod 3^n)
  refine ⟨fun play => P ⟨((Pequiv.symm (play.getLastD (zeroVtx n))).val + 1) % (3 ^ n),
    Nat.mod_lt _ h3n_pos⟩, ?_⟩
  intro play hlen hstart_play hnodup hadj_play hmaximal hbob
  -- Parity of play vertices from L5
  have hplay_parity : ∀ (k : ℕ) (hk : k < play.length),
      (∑ j : Fin n, (play.get ⟨k, hk⟩ j).val) % 2 = k % 2 :=
    fun k hk => lem_20260315232302_10f794c0 n hn play.length hlen
      (fun i => play.get i) hstart_play hadj_play ⟨k, hk⟩
  -- P-index parity = position parity
  have hindex_parity : ∀ (k : ℕ) (hk : k < play.length),
      (Pequiv.symm (play.get ⟨k, hk⟩)).val % 2 = k % 2 := by
    intro k hk
    have h1 := hplay_parity k hk
    have h2 := hparity (Pequiv.symm (play.get ⟨k, hk⟩))
    have h3 : P (Pequiv.symm (play.get ⟨k, hk⟩)) = play.get ⟨k, hk⟩ :=
      Pequiv.apply_symm_apply _
    rw [h3] at h2; omega
  -- 3^n is odd
  have h3n_odd : (3 ^ n) % 2 = 1 := Nat.odd_iff.mp (Odd.pow (by norm_num : Odd 3))
  -- Odd P-index implies successor exists
  have hodd_succ : ∀ (m : Fin (3 ^ n)), m.val % 2 = 1 → m.val + 1 < 3 ^ n := by
    intro m hm; have := m.isLt; omega
  -- Key lemma: getLastD of nonempty list take
  have hgetLastD_take : ∀ (j : ℕ) (hj : 2 * j + 2 < play.length),
      (List.take (2 * j + 2) play).getLastD (zeroVtx n) =
      play.get ⟨2 * j + 1, by omega⟩ := by
    intro j hj
    have hne : List.take (2 * j + 2) play ≠ [] := by
      have hlen2 : (List.take (2 * j + 2) play).length = 2 * j + 2 :=
        List.length_take_of_le (by omega)
      intro h; simp [h] at hlen2
    rw [List.getLastD_eq_getLast? , List.getLast?_eq_some_getLast hne, Option.getD_some,
        List.getLast_eq_getElem]
    simp only [List.length_take, show min (2 * j + 2) play.length = 2 * j + 2 from by omega,
      show (2 * j + 2) - 1 = 2 * j + 1 from by omega]
    exact (List.getElem_take' (by omega) (by omega)).symm
  -- σ characterization: for valid Bob turns
  have hσ_char : ∀ (j : ℕ) (hj : 2 * j + 2 < play.length),
      (fun play => P ⟨((Pequiv.symm (play.getLastD (zeroVtx n))).val + 1) % (3 ^ n),
        Nat.mod_lt _ h3n_pos⟩) (List.take (2 * j + 2) play) =
      P ⟨((Pequiv.symm (play.get ⟨2 * j + 1, by omega⟩)).val + 1) % (3 ^ n),
        Nat.mod_lt _ h3n_pos⟩ := by
    intro j hj
    simp only [hgetLastD_take j hj]
  -- Proof by contradiction: suppose play.length - 1 is odd
  by_contra h_ne
  have h_odd_last : (play.length - 1) % 2 = 1 := by omega
  have hL_lt : play.length - 1 < play.length := by omega
  -- m = P-index of last vertex
  set m := Pequiv.symm (play.get ⟨play.length - 1, hL_lt⟩) with hm_def
  have hm_odd : m.val % 2 = 1 := by
    rw [hm_def]; exact (hindex_parity _ hL_lt).trans h_odd_last
  have hm_succ : m.val + 1 < 3 ^ n := hodd_succ m hm_odd
  -- P(m) = last vertex
  have hPm : P m = play.get ⟨play.length - 1, hL_lt⟩ := Pequiv.apply_symm_apply _
  -- P(m+1) is adjacent to last vertex
  have hadj_m : HnAdj (play.get ⟨play.length - 1, hL_lt⟩) (P ⟨m.val + 1, hm_succ⟩) := by
    rw [← hPm]
    have h := hadj m.val hm_succ
    convert h using 2 <;> exact congr_arg P (Fin.ext rfl)
  -- P(m+1) ∈ play by maximality
  have hmem := hmaximal _ hadj_m
  rw [List.mem_iff_get] at hmem
  obtain ⟨⟨k, hk_lt⟩, hk_eq⟩ := hmem
  -- P-index of play[k] is m+1
  have hPinv_k : Pequiv.symm (play.get ⟨k, hk_lt⟩) = ⟨m.val + 1, hm_succ⟩ := by
    apply Pequiv.injective; rw [Equiv.apply_symm_apply]; exact hk_eq
  -- k is even
  have hk_even : k % 2 = 0 := by
    have h1 := hindex_parity k hk_lt; rw [hPinv_k] at h1; simp at h1; omega
  -- k ≠ 0
  have hk_ne0 : k ≠ 0 := by
    intro heq; subst heq
    rw [hstart_play] at hk_eq
    have h2 : P ⟨0, by positivity⟩ = P ⟨m.val + 1, hm_succ⟩ := by rw [← hk_eq, hstart]
    have h3 : (⟨0, by positivity⟩ : Fin (3 ^ n)) = ⟨m.val + 1, hm_succ⟩ := hbij.1 h2
    simp only [Fin.mk.injEq] at h3
    omega
  -- k ≥ 2 and k = 2*jj+2
  set jj := k / 2 - 1 with hjj_def
  have hk_eq_2jj2 : k = 2 * jj + 2 := by omega
  have hjj_lt : 2 * jj + 2 < play.length := by omega
  -- From hbob: play[2jj+2] = σ(play.take(2jj+2))
  have hbob_jj := hbob jj hjj_lt
  -- From σ characterization + hbob:
  -- play[2jj+2] = P ⟨((Pequiv.symm (play[2jj+1])).val + 1) % 3^n, _⟩
  have hσ_eq := hσ_char jj hjj_lt
  -- play[k] = play[2jj+2]
  have hk_get : play.get ⟨k, hk_lt⟩ = play.get ⟨2 * jj + 2, by omega⟩ :=
    congr_arg play.get (Fin.ext (by omega))
  -- So P ⟨m+1, _⟩ = P ⟨(Pequiv.symm(play[2jj+1])+1) % 3^n, _⟩
  have h_prev_lt : 2 * jj + 1 < play.length := by omega
  have h_eq_P : P ⟨m.val + 1, hm_succ⟩ =
      P ⟨((Pequiv.symm (play.get ⟨2 * jj + 1, by omega⟩)).val + 1) % (3 ^ n),
        Nat.mod_lt _ h3n_pos⟩ := by
    rw [← hk_eq, hk_get, hbob_jj, hσ_eq]
  -- By injectivity of P
  have h_fin_eq := hbij.1 h_eq_P
  have h_val_eq : m.val + 1 =
      ((Pequiv.symm (play.get ⟨2 * jj + 1, by omega⟩)).val + 1) % (3 ^ n) :=
    Fin.val_eq_of_eq h_fin_eq
  -- The mod reduces since prev index is odd and < 3^n
  have h_prev_idx_odd : (Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩)).val % 2 = 1 :=
    (hindex_parity _ h_prev_lt).trans (by omega)
  have h_prev_succ : (Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩)).val + 1 < 3 ^ n :=
    hodd_succ _ h_prev_idx_odd
  have h_mod_id : ((Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩)).val + 1) % (3 ^ n) =
      (Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩)).val + 1 :=
    Nat.mod_eq_of_lt h_prev_succ
  rw [h_mod_id] at h_val_eq
  -- m = Pequiv.symm(play[2jj+1])
  have h_m_val : m.val = (Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩)).val := by omega
  -- play[2jj+1] = P(m) = play[last]
  have h_prev_is_Pm : play.get ⟨2 * jj + 1, h_prev_lt⟩ = P m := by
    have h : Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩) = m := Fin.ext h_m_val.symm
    calc play.get ⟨2 * jj + 1, h_prev_lt⟩
        = Pequiv (Pequiv.symm (play.get ⟨2 * jj + 1, h_prev_lt⟩)) :=
          (Pequiv.apply_symm_apply _).symm
      _ = Pequiv m := congr_arg Pequiv h
      _ = P m := Equiv.ofBijective_apply P hbij m
  have h_dup : play.get ⟨2 * jj + 1, h_prev_lt⟩ = play.get ⟨play.length - 1, hL_lt⟩ := by
    rw [h_prev_is_Pm, ← hPm]
  -- 2jj+1 ≠ play.length-1 (since 2jj+2 ≤ play.length-1, so 2jj+1 < play.length-1)
  have h_ne_idx : (2 * jj + 1 : ℕ) ≠ play.length - 1 := by omega
  -- Contradicts Nodup
  exact h_ne_idx (Fin.val_eq_of_eq ((List.nodup_iff_injective_get.mp hnodup) h_dup))


/- ===== Root ===== -/
theorem root_prob_20260315224206_9f8facc7 (n : ℕ) (hn : n ≥ 1) : BobWinsGame n := by
  obtain ⟨P, hbij, hstart, hadj⟩ := lem_20260315232216_4cebae53 n hn
  have hparity := lem_20260315232302_10f794c0 n hn (3 ^ n) (Nat.one_le_pow n 3 (by omega)) P hstart hadj
  exact lem_20260315232346_5554102e n hn P hbij hstart hadj hparity
