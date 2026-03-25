import Mathlib

open scoped BigOperators

/- ===== Statements ===== -/
/-! ## Sequence definitions for Putnam A1 -/

mutual
def seqM (m₀ n₀ : ℕ) : ℕ → ℕ
  | 0 => m₀
  | k + 1 => (2 * seqM m₀ n₀ k + 1) / Nat.gcd (2 * seqM m₀ n₀ k + 1) (2 * seqN m₀ n₀ k + 1)
def seqN (m₀ n₀ : ℕ) : ℕ → ℕ
  | 0 => n₀
  | k + 1 => (2 * seqN m₀ n₀ k + 1) / Nat.gcd (2 * seqM m₀ n₀ k + 1) (2 * seqN m₀ n₀ k + 1)
end

def seqD (m₀ n₀ : ℕ) (k : ℕ) : ℕ :=
  Nat.gcd (2 * seqM m₀ n₀ k + 1) (2 * seqN m₀ n₀ k + 1)

open Finset BigOperators in
/-! ## Lemma declarations -/

/- ===== Lemmas ===== -/

theorem lem_20260315033205_c6218886 (m₀ n₀ : ℕ) (hm : 0 < m₀) (hn : 0 < n₀) (hmn : m₀ ≠ n₀) (j : ℕ) : seqD m₀ n₀ j = 1 ↔ Nat.Coprime (2 * seqM m₀ n₀ j + 1) (2 * seqN m₀ n₀ j + 1) := by
  unfold seqD Nat.Coprime
  exact Iff.rfl

theorem lem_20260315032944_124a8204 (a b : ℕ) (ha : 0 < a) (hb : 0 < b) (hab : a ≠ b) : (Nat.gcd (2 * a + 1) (2 * b + 1) : ℤ) ∣ 2 * ((a : ℤ) - (b : ℤ)) ∧ (Nat.gcd (2 * a + 1) (2 * b + 1) : ℤ) * ((2 * (a : ℤ) + 1) / (Nat.gcd (2 * a + 1) (2 * b + 1) : ℤ) - (2 * (b : ℤ) + 1) / (Nat.gcd (2 * a + 1) (2 * b + 1) : ℤ)) = 2 * ((a : ℤ) - (b : ℤ)) := by
  set d := Nat.gcd (2 * a + 1) (2 * b + 1) with hd_def
  have hd1 : (d : ℤ) ∣ (2 * (a : ℤ) + 1) := by
    exact_mod_cast Nat.gcd_dvd_left (2 * a + 1) (2 * b + 1)
  have hd2 : (d : ℤ) ∣ (2 * (b : ℤ) + 1) := by
    exact_mod_cast Nat.gcd_dvd_right (2 * a + 1) (2 * b + 1)
  have hdiff : (d : ℤ) ∣ 2 * ((a : ℤ) - (b : ℤ)) := by
    have : (d : ℤ) ∣ ((2 * (a : ℤ) + 1) - (2 * (b : ℤ) + 1)) := dvd_sub hd1 hd2
    convert this using 1; ring
  constructor
  · exact hdiff
  · rw [mul_sub, Int.mul_ediv_cancel' hd1, Int.mul_ediv_cancel' hd2]
    ring

theorem lem_20260315033109_7360bff1 (N : ℕ) (hN : 0 < N) (e : ℕ → ℕ) (he_pos : ∀ i, 0 < e i) (he_odd : ∀ i, ¬ 2 ∣ e i) (hdvd : ∀ r : ℕ, 0 < r → (∏ i ∈ Finset.range r, e i) ∣ 2 ^ r * N) : ∀ r : ℕ, 0 < r → (∏ i ∈ Finset.range r, e i) ∣ N := by
  intro r hr
  have hcop : Nat.Coprime (∏ i ∈ Finset.range r, e i) (2 ^ r) := by
    apply Nat.Coprime.prod_left
    intro i _
    apply Nat.Coprime.pow_right
    rw [Nat.coprime_comm]
    exact (Nat.Prime.coprime_iff_not_dvd (by norm_num)).mpr (he_odd i)
  have h := hdvd r hr
  rw [mul_comm] at h
  exact Nat.Coprime.dvd_of_dvd_mul_right hcop h

theorem lem_20260315033043_bd72d344 (m₀ n₀ : ℕ) (hm : 0 < m₀) (hn : 0 < n₀) (hmn : m₀ ≠ n₀) : ∀ k : ℕ, 0 < k → (↑(∏ i ∈ Finset.range k, seqD m₀ n₀ i) : ℤ) ∣ 2 ^ k * ((m₀ : ℤ) - (n₀ : ℤ)) := by
  -- Prove stronger identity: ∏_{i<k} d_i * (M_k - N_k) = 2^k * (m₀ - n₀)
  suffices key : ∀ k : ℕ, (↑(∏ i ∈ Finset.range k, seqD m₀ n₀ i) : ℤ) *
      ((seqM m₀ n₀ k : ℤ) - (seqN m₀ n₀ k : ℤ)) =
      2 ^ k * ((m₀ : ℤ) - (n₀ : ℤ)) by
    intro k hk
    exact ⟨(seqM m₀ n₀ k : ℤ) - (seqN m₀ n₀ k : ℤ), (key k).symm⟩
  -- One-step relation: d_j * (M_{j+1} - N_{j+1}) = 2 * (M_j - N_j)
  have one_step : ∀ j : ℕ, (seqD m₀ n₀ j : ℤ) *
      ((seqM m₀ n₀ (j + 1) : ℤ) - (seqN m₀ n₀ (j + 1) : ℤ)) =
      2 * ((seqM m₀ n₀ j : ℤ) - (seqN m₀ n₀ j : ℤ)) := by
    intro j
    have hd_dvd_m : seqD m₀ n₀ j ∣ (2 * seqM m₀ n₀ j + 1) := by
      simp only [seqD]; exact Nat.gcd_dvd_left _ _
    have hd_dvd_n : seqD m₀ n₀ j ∣ (2 * seqN m₀ n₀ j + 1) := by
      simp only [seqD]; exact Nat.gcd_dvd_right _ _
    have hM_eq : seqM m₀ n₀ (j + 1) = (2 * seqM m₀ n₀ j + 1) / seqD m₀ n₀ j := by
      simp only [seqM, seqD]
    have hN_eq : seqN m₀ n₀ (j + 1) = (2 * seqN m₀ n₀ j + 1) / seqD m₀ n₀ j := by
      simp only [seqN, seqD]
    have hM_mul : seqD m₀ n₀ j * seqM m₀ n₀ (j + 1) = 2 * seqM m₀ n₀ j + 1 := by
      rw [hM_eq, mul_comm, Nat.div_mul_cancel hd_dvd_m]
    have hN_mul : seqD m₀ n₀ j * seqN m₀ n₀ (j + 1) = 2 * seqN m₀ n₀ j + 1 := by
      rw [hN_eq, mul_comm, Nat.div_mul_cancel hd_dvd_n]
    have hM_z : (seqD m₀ n₀ j : ℤ) * (seqM m₀ n₀ (j + 1) : ℤ) =
        2 * (seqM m₀ n₀ j : ℤ) + 1 := by exact_mod_cast hM_mul
    have hN_z : (seqD m₀ n₀ j : ℤ) * (seqN m₀ n₀ (j + 1) : ℤ) =
        2 * (seqN m₀ n₀ j : ℤ) + 1 := by exact_mod_cast hN_mul
    linarith [mul_sub (seqD m₀ n₀ j : ℤ) (seqM m₀ n₀ (j + 1) : ℤ) (seqN m₀ n₀ (j + 1) : ℤ)]
  -- Induction on k
  intro k
  induction k with
  | zero => simp [seqM, seqN]
  | succ k ih =>
    rw [Finset.prod_range_succ, Nat.cast_mul]
    calc (↑(∏ i ∈ Finset.range k, seqD m₀ n₀ i) : ℤ) * ↑(seqD m₀ n₀ k) *
          ((seqM m₀ n₀ (k + 1) : ℤ) - (seqN m₀ n₀ (k + 1) : ℤ))
        = (↑(∏ i ∈ Finset.range k, seqD m₀ n₀ i) : ℤ) *
          (↑(seqD m₀ n₀ k) * ((seqM m₀ n₀ (k + 1) : ℤ) - (seqN m₀ n₀ (k + 1) : ℤ))) := by ring
      _ = (↑(∏ i ∈ Finset.range k, seqD m₀ n₀ i) : ℤ) *
          (2 * ((seqM m₀ n₀ k : ℤ) - (seqN m₀ n₀ k : ℤ))) := by rw [one_step k]
      _ = 2 * ((↑(∏ i ∈ Finset.range k, seqD m₀ n₀ i) : ℤ) *
          ((seqM m₀ n₀ k : ℤ) - (seqN m₀ n₀ k : ℤ))) := by ring
      _ = 2 * (2 ^ k * ((m₀ : ℤ) - (n₀ : ℤ))) := by rw [ih]
      _ = 2 ^ (k + 1) * ((m₀ : ℤ) - (n₀ : ℤ)) := by ring

theorem lem_20260315033008_b1ba019f (m₀ n₀ : ℕ) (hm : 0 < m₀) (hn : 0 < n₀) (hmn : m₀ ≠ n₀) : (∀ k : ℕ, (seqD m₀ n₀ k : ℤ) * ((seqM m₀ n₀ (k + 1) : ℤ) - (seqN m₀ n₀ (k + 1) : ℤ)) = 2 * ((seqM m₀ n₀ k : ℤ) - (seqN m₀ n₀ k : ℤ))) ∧ (∀ k : ℕ, seqM m₀ n₀ k ≠ seqN m₀ n₀ k) := by
  have key : ∀ k : ℕ, (seqD m₀ n₀ k : ℤ) * ((seqM m₀ n₀ (k + 1) : ℤ) - (seqN m₀ n₀ (k + 1) : ℤ)) = 2 * ((seqM m₀ n₀ k : ℤ) - (seqN m₀ n₀ k : ℤ)) := by
    intro k
    set M := seqM m₀ n₀ k with hM_def
    set N := seqN m₀ n₀ k with hN_def
    set d := Nat.gcd (2 * M + 1) (2 * N + 1) with hd_def
    have hd_seqD : seqD m₀ n₀ k = d := rfl
    have hdM : d ∣ (2 * M + 1) := Nat.gcd_dvd_left (2 * M + 1) (2 * N + 1)
    have hdN : d ∣ (2 * N + 1) := Nat.gcd_dvd_right (2 * M + 1) (2 * N + 1)
    have hM_succ : seqM m₀ n₀ (k + 1) = (2 * M + 1) / d := rfl
    have hN_succ : seqN m₀ n₀ (k + 1) = (2 * N + 1) / d := rfl
    rw [hd_seqD, hM_succ, hN_succ, mul_sub]
    have h1 : (d : ℤ) * (↑((2 * M + 1) / d) : ℤ) = ↑(2 * M + 1) := by
      exact_mod_cast Nat.mul_div_cancel' hdM
    have h2 : (d : ℤ) * (↑((2 * N + 1) / d) : ℤ) = ↑(2 * N + 1) := by
      exact_mod_cast Nat.mul_div_cancel' hdN
    rw [h1, h2]
    push_cast; ring
  constructor
  · exact key
  · intro k
    induction k with
    | zero => simp only [seqM, seqN]; exact hmn
    | succ k ih =>
      intro heq
      have hk := key k
      have heq_int : (seqM m₀ n₀ (k + 1) : ℤ) = (seqN m₀ n₀ (k + 1) : ℤ) := by exact_mod_cast heq
      rw [heq_int, sub_self, mul_zero] at hk
      have hmn_k : (seqM m₀ n₀ k : ℤ) = (seqN m₀ n₀ k : ℤ) := by linarith
      have : seqM m₀ n₀ k = seqN m₀ n₀ k := by exact_mod_cast hmn_k
      exact ih this

theorem lem_20260315033134_415173a4 (N : ℕ) (hN : 0 < N) (e : ℕ → ℕ) (he_pos : ∀ i, 1 ≤ e i) (hdvd : ∀ r : ℕ, 0 < r → (∏ i ∈ Finset.range r, e i) ∣ N) : Set.Finite {i : ℕ | 1 < e i} := by
  by_contra hinf
  have hinf : Set.Infinite {i : ℕ | 1 < e i} := hinf
  obtain ⟨S, hS, hcard⟩ := hinf.exists_subset_card_eq (N + 1)
  have hS_ne : S.Nonempty := by
    rw [Finset.nonempty_iff_ne_empty]
    intro h; subst h; simp at hcard
  set r := S.sup' hS_ne id + 1 with hr_def
  have hr_pos : 0 < r := Nat.succ_pos _
  have hS_sub : S ⊆ Finset.range r := by
    intro x hx
    simp only [Finset.mem_range, hr_def]
    exact Nat.lt_succ_of_le (Finset.le_sup' id hx)
  have h1 : ∏ i ∈ S, e i ≤ ∏ i ∈ Finset.range r, e i :=
    Finset.prod_le_prod_of_subset_of_one_le' hS_sub (fun i _ _ => he_pos i)
  have h2 : 2 ^ S.card ≤ ∏ i ∈ S, e i := by
    rw [← Finset.prod_const]
    apply Finset.prod_le_prod (fun _ _ => by omega)
    intro i hi
    exact hS (Finset.mem_coe.mpr hi)
  have h3 : 2 ^ (N + 1) ≤ ∏ i ∈ Finset.range r, e i := by
    rw [← hcard]; exact le_trans h2 h1
  have h4 : ∏ i ∈ Finset.range r, e i ≤ N := Nat.le_of_dvd hN (hdvd r hr_pos)
  have h5 : N < 2 ^ (N + 1) := by
    calc N < 2 ^ N := Nat.lt_two_pow_self
    _ ≤ 2 ^ (N + 1) := Nat.pow_le_pow_right (by omega) (by omega)
  omega

theorem lem_20260315032921_8bcb0ad7 (a b m n : ℕ) (ha : 0 < a) (hb : 0 < b) (hm : 0 < m) (hn : 0 < n) (heq : m * (2 * b + 1) = n * (2 * a + 1)) (hcop : Nat.Coprime m n) : m = (2 * a + 1) / Nat.gcd (2 * a + 1) (2 * b + 1) ∧ n = (2 * b + 1) / Nat.gcd (2 * a + 1) (2 * b + 1) ∧ ¬ 2 ∣ Nat.gcd (2 * a + 1) (2 * b + 1) := by
  set A := 2 * a + 1 with hA_def
  set B := 2 * b + 1 with hB_def
  have heq' : n * A = m * B := by linarith
  -- m ∣ A: from heq, m * B = n * A, so m ∣ n * A; coprimality gives m ∣ A
  have hm_dvd_A : m ∣ A := by
    have h1 : m ∣ n * A := ⟨B, by linarith⟩
    exact hcop.dvd_of_dvd_mul_left h1
  -- n ∣ B: from heq, n * A = m * B, so n ∣ m * B; coprimality gives n ∣ B
  have hn_dvd_B : n ∣ B := by
    have h1 : n ∣ m * B := ⟨A, by linarith⟩
    exact hcop.symm.dvd_of_dvd_mul_left h1
  obtain ⟨t, ht⟩ := hm_dvd_A  -- A = m * t
  obtain ⟨s, hs⟩ := hn_dvd_B  -- B = n * s
  -- Show s = t
  have hst : s = t := by
    have key : m * B = m * (n * t) := by
      calc m * B = n * A := by linarith
        _ = n * (m * t) := by rw [ht]
        _ = m * (n * t) := by ring
    have h1 : B = n * t := Nat.eq_of_mul_eq_mul_left hm key
    have h2 : n * s = n * t := by linarith
    exact Nat.eq_of_mul_eq_mul_left hn h2
  rw [hst] at hs  -- now hs : B = n * t
  -- t > 0
  have ht_pos : 0 < t := by
    by_contra h
    push_neg at h
    interval_cases t
    simp at ht; omega
  -- gcd A B = t
  have hd_eq : Nat.gcd A B = t := by
    rw [ht, hs]
    rw [show m * t = t * m from mul_comm m t, show n * t = t * n from mul_comm n t]
    rw [Nat.gcd_mul_left]
    rw [hcop.gcd_eq_one]
    simp
  refine ⟨?_, ?_, ?_⟩
  · -- m = A / gcd A B
    rw [hd_eq, ht, Nat.mul_div_cancel _ ht_pos]
  · -- n = B / gcd A B
    rw [hd_eq, hs, Nat.mul_div_cancel _ ht_pos]
  · -- gcd A B is odd
    rw [hd_eq]
    intro h2
    have h2A : 2 ∣ A := dvd_trans h2 (by rw [← hd_eq]; exact Nat.gcd_dvd_left A B)
    simp [hA_def] at h2A


/- ===== Root ===== -/
open Finset BigOperators

theorem root_prob_20260315030719_fffb3b00 (m₀ n₀ : ℕ) (hm : 0 < m₀) (hn : 0 < n₀) (hmn : m₀ ≠ n₀) : ∃ K : ℕ, ∀ k : ℕ, K ≤ k → Nat.Coprime (2 * seqM m₀ n₀ k + 1) (2 * seqN m₀ n₀ k + 1) := by
  -- seqD positivity and oddness
  have hd_pos : ∀ i, 0 < seqD m₀ n₀ i := fun i => by
    simp only [seqD]; exact Nat.gcd_pos_of_pos_left _ (by omega)
  have hd_odd : ∀ i, ¬2 ∣ seqD m₀ n₀ i := fun i => by
    simp only [seqD]; intro h
    have := dvd_trans h (Nat.gcd_dvd_left _ _); omega
  -- N = |m₀ - n₀| > 0
  have hN_pos : 0 < Int.natAbs ((m₀ : ℤ) - ↑n₀) := by
    rw [Int.natAbs_pos, sub_ne_zero]; exact_mod_cast hmn
  -- L4: product of seqD divides 2^k * (m₀ - n₀) in ℤ
  have hprod_dvd_int := lem_20260315033043_bd72d344 m₀ n₀ hm hn hmn
  -- Convert to ℕ: product divides 2^k * |m₀ - n₀|
  have hprod_dvd_nat : ∀ r, 0 < r →
      (∏ i ∈ range r, seqD m₀ n₀ i) ∣ 2 ^ r * Int.natAbs ((m₀ : ℤ) - ↑n₀) := by
    intro r hr
    obtain ⟨c, hc⟩ := hprod_dvd_int r hr
    refine ⟨c.natAbs, ?_⟩
    have h1 := congr_arg Int.natAbs hc
    simp only [Int.natAbs_mul, Int.natAbs_pow, Int.natAbs_natCast] at h1
    norm_num at h1
    exact h1
  -- L5: remove 2^k via oddness → product divides |m₀ - n₀|
  have hprod_dvd_N := lem_20260315033109_7360bff1 _ hN_pos (seqD m₀ n₀) hd_pos hd_odd hprod_dvd_nat
  -- L6: finitely many seqD > 1
  have hfin := lem_20260315033134_415173a4 _ hN_pos (seqD m₀ n₀)
    (fun i => by have := hd_pos i; omega) hprod_dvd_N
  -- Extract threshold K from finite set bound
  obtain ⟨K, hK⟩ := hfin.bddAbove
  use K + 1
  intro k hk
  -- L7: seqD = 1 ↔ coprimality
  rw [← lem_20260315033205_c6218886 m₀ n₀ hm hn hmn k]
  by_contra hne
  have h1 : 1 < seqD m₀ n₀ k := by have := hd_pos k; omega
  have := hK h1
  omega
