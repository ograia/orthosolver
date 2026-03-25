# Task: Complete a Lean 4 proof

You are given a partially-proven Lean 4 theorem with 3 remaining `sorry` placeholders. Your task is to replace ALL `sorry` with valid Lean 4 tactic proofs. The result must compile with no errors against Lean 4 + Mathlib.

## Rules
- Keep the theorem signature EXACTLY as given (do not change the statement)
- Replace all 3 `sorry` placeholders with valid proofs
- You may add helper `have` statements, auxiliary lemmas, or restructure the proof body
- The file uses `import Mathlib` — all of Mathlib is available
- `native_decide` is allowed for decidable propositions
- Output the COMPLETE file (all imports, trusted context, and the full theorem)

## Natural Language Proof

Let n=2025, m=1012, θ = 2πm/n, c = √(−cos θ). Define v_i = (cos(iθ), sin(iθ), c) ∈ ℝ³.

**Dot product formula:** v_i · v_j = cos(iθ)cos(jθ) + sin(iθ)sin(jθ) + c² = cos((i−j)θ) + c² = cos(dθ) − cos θ (since c² = −cos θ). So v_i · v_j = 0 ⟺ cos(dθ) = cos θ.

**Forward direction (Sorry 1):** cos(dθ) = cos(θ) means dθ = 2πt ± θ for some integer t. Substituting θ = 2πm/n: m(d∓1) = tn. Since gcd(m,n) = gcd(1012,2025) = 1, we get n | (d∓1), i.e., d ≡ ±1 (mod n). With |d| ≤ n−1 and d ≠ 0, this forces |d| ∈ {1, n−1}.

**Backward direction (Sorry 2):** If |d| = 1: cos(±θ) = cos(θ). If |d| = n−1: cos((n−1)θ) = cos(nθ − θ) = cos(2πm − θ) = cos(θ).

**Non-parallel contradiction (Sorry 3):** From cos((i−j)θ) = 1, we get (i−j)θ = 2πt, so (i−j)m = tn. Since gcd(m,n) = 1, n | (i−j). But |i−j| ≤ n−1, so i−j = 0, contradicting i ≠ j.

## Starting Point (compile this file with the sorries filled in)

```lean
import Mathlib
import Orthos.Statements

-- Trusted context: previously-proven lemma signatures
theorem lem_20260315032510_a6164cbd : ¬ ∃ A : Fin 2025 → Matrix (Fin 1) (Fin 1) ℝ, ∀ i j : Fin 2025, A i * A j = A j * A i ↔ ((i : ℤ) - (j : ℤ)).natAbs ∈ ({0, 1, 2024} : Finset ℕ) := by sorry
theorem lem_20260315032543_0386ddd1 (B : Matrix (Fin 2) (Fin 2) ℝ) (hB : ¬ ∃ c : ℝ, B = c • (1 : Matrix (Fin 2) (Fin 2) ℝ)) : (∀ X : Matrix (Fin 2) (Fin 2) ℝ, X * B = B * X ↔ ∃ α β : ℝ, X = α • (1 : Matrix (Fin 2) (Fin 2) ℝ) + β • B) ∧ (∀ X : Matrix (Fin 2) (Fin 2) ℝ, X * B = B * X → ∀ α₁ β₁ α₂ β₂ : ℝ, X = α₁ • (1 : Matrix (Fin 2) (Fin 2) ℝ) + β₁ • B → X = α₂ • (1 : Matrix (Fin 2) (Fin 2) ℝ) + β₂ • B → α₁ = α₂ ∧ β₁ = β₂) ∧ (∀ X Y : Matrix (Fin 2) (Fin 2) ℝ, X * B = B * X → Y * B = B * Y → X * Y = Y * X) := by sorry
theorem lem_20260315032605_ec8ca1a1 (A : Fin 2025 → Matrix (Fin 2) (Fin 2) ℝ) (hA : ∀ i j : Fin 2025, A i * A j = A j * A i ↔ ((i : ℤ) - (j : ℤ)).natAbs ∈ ({0, 1, 2024} : Finset ℕ)) : ∀ i : Fin 2025, ¬ ∃ c : ℝ, A i = c • (1 : Matrix (Fin 2) (Fin 2) ℝ) := by sorry
theorem lem_20260315032632_1d533fda : ¬ ∃ A : Fin 2025 → Matrix (Fin 2) (Fin 2) ℝ, ∀ i j : Fin 2025, A i * A j = A j * A i ↔ ((i : ℤ) - (j : ℤ)).natAbs ∈ ({0, 1, 2024} : Finset ℕ) := by sorry

theorem lem_20260315032701_8f502a27 :
    let θ : ℝ := 2 * Real.pi * 1012 / 2025
    let c : ℝ := Real.sqrt (-Real.cos θ)
    let v : Fin 2025 → Fin 3 → ℝ := fun i =>
      ![Real.cos (((i : ℕ) : ℝ) * θ), Real.sin (((i : ℕ) : ℝ) * θ), c]
    (∀ i j : Fin 2025, i ≠ j →
      (dotProduct (v i) (v j) = 0 ↔
        ((i : ℤ) - (j : ℤ)).natAbs ∈ ({1, 2024} : Finset ℕ))) ∧
    (∀ i j : Fin 2025, i ≠ j → ¬ ∃ t : ℝ, v i = t • v j) := by
  set θ : ℝ := 2 * Real.pi * 1012 / 2025 with hθ_def
  set c : ℝ := Real.sqrt (-Real.cos θ) with hc_def
  set v : Fin 2025 → Fin 3 → ℝ := fun i =>
    ![Real.cos (((i : ℕ) : ℝ) * θ), Real.sin (((i : ℕ) : ℝ) * θ), c] with hv_def
  -- θ = π - π/2025
  have hθ_eq : θ = Real.pi - Real.pi / 2025 := by rw [hθ_def]; ring
  -- cos θ < 0
  have hcos_neg : Real.cos θ < 0 := by
    rw [hθ_eq, Real.cos_pi_sub]
    have hpi_pos : Real.pi > 0 := Real.pi_pos
    have h1 : (0 : ℝ) < Real.pi / 2025 := by positivity
    have h2 : Real.pi / 2025 < Real.pi / 2 := by
      apply div_lt_div_of_pos_left hpi_pos (by positivity) (by norm_num)
    linarith [Real.cos_pos_of_mem_Ioo (show Real.pi / 2025 ∈ Set.Ioo (-(Real.pi / 2)) (Real.pi / 2) from
      ⟨by linarith, by linarith⟩)]
  have hneg_cos_pos : -Real.cos θ > 0 := by linarith
  have hc_pos : c > 0 := by rw [hc_def]; exact Real.sqrt_pos.mpr hneg_cos_pos
  have hc_sq : c ^ 2 = -Real.cos θ := by rw [hc_def]; exact Real.sq_sqrt (le_of_lt hneg_cos_pos)
  have hcc : c * c = -Real.cos θ := by nlinarith [hc_sq]
  -- θ * 2025 = 2π * 1012
  have hθ_period : θ * 2025 = 2 * Real.pi * 1012 := by rw [hθ_def]; ring
  have hgcd : Nat.gcd 1012 2025 = 1 := by native_decide
  -- Helper: v i at index 2 equals c
  have hv_idx2 : ∀ i : Fin 2025, v i 2 = c := fun _ => rfl
  -- Dot product formula: v_i · v_j = cos((i-j)θ) - cos θ
  have hdot_formula : ∀ i j : Fin 2025,
      dotProduct (v i) (v j) = Real.cos ((↑(i : ℕ) - ↑(j : ℕ)) * θ) - Real.cos θ := by
    intro i j
    simp only [hv_def, dotProduct, Fin.sum_univ_three, Matrix.cons_val_zero, Matrix.cons_val_one,
      Matrix.head_cons]
    have h2i : (![Real.cos (↑↑i * θ), Real.sin (↑↑i * θ), c] : Fin 3 → ℝ) 2 = c := rfl
    have h2j : (![Real.cos (↑↑j * θ), Real.sin (↑↑j * θ), c] : Fin 3 → ℝ) 2 = c := rfl
    rw [h2i, h2j]
    rw [show (↑↑i - ↑↑j) * θ = ↑↑i * θ - ↑↑j * θ from by ring]
    rw [Real.cos_sub]
    linarith [hcc]
  constructor
  · -- Part 1: orthogonality iff |i-j| ∈ {1, 2024}
    intro i j hij
    rw [hdot_formula]
    constructor
    · -- Forward: dot = 0 → |i-j| ∈ {1,2024}
      intro hdot
      have hcos_eq : Real.cos ((↑(i : ℕ) - ↑(j : ℕ)) * θ) = Real.cos θ := by linarith
      -- TODO: From cos(dθ) = cos(θ), deduce dθ = 2πt ± θ for some t ∈ ℤ
      -- Then substitute θ = 2πm/n to get m(d∓1) = tn
      -- Use gcd(m,n) = 1 to conclude n | (d∓1)
      -- With |d| ≤ n-1 and d ≠ 0, get |d| ∈ {1, n-1}
      sorry
    · -- Backward: |i-j| ∈ {1,2024} → dot = 0
      intro hmem
      -- If |d| = 1: cos(±θ) = cos(θ) by even symmetry
      -- If |d| = n-1 = 2024: cos(2024θ) = cos(2025θ - θ) = cos(2πm - θ) = cos(θ)
      sorry
  · -- Part 2: not parallel
    intro i j hij ⟨t, ht⟩
    have hv_eq : ∀ k : Fin 3, v i k = t * v j k := by
      intro k; have := congr_fun ht k; simp [Pi.smul_apply, smul_eq_mul] at this; exact this
    have hc_eq : c = t * c := by
      have h2 := hv_eq 2
      rw [hv_idx2, hv_idx2] at h2
      exact h2
    have ht_eq : t = 1 := by nlinarith [hc_pos]
    have hvi_eq_vj : v i = v j := by rw [ht_eq, one_smul] at ht; exact ht
    have hcos_eq : Real.cos (↑(i : ℕ) * θ) = Real.cos (↑(j : ℕ) * θ) := by
      have := congr_fun hvi_eq_vj 0; simp only [hv_def, Matrix.cons_val_zero] at this; exact this
    have hsin_eq : Real.sin (↑(i : ℕ) * θ) = Real.sin (↑(j : ℕ) * θ) := by
      have := congr_fun hvi_eq_vj 1
      simp only [hv_def, Matrix.cons_val_one, Matrix.head_cons] at this; exact this
    have hcos_diff_one : Real.cos ((↑(i : ℕ) - ↑(j : ℕ)) * θ) = 1 := by
      rw [show (↑↑i - ↑↑j) * θ = ↑↑i * θ - ↑↑j * θ from by ring, Real.cos_sub,
        hcos_eq, hsin_eq]
      have h := Real.sin_sq_add_cos_sq (↑(j : ℕ) * θ)
      rw [sq, sq] at h; linarith
    -- TODO: From cos((i-j)θ) = 1, deduce (i-j)θ = 2πt
    -- Substitute θ = 2πm/n to get (i-j)m = tn
    -- Use gcd(m,n) = 1 to conclude n | (i-j)
    -- But |i-j| ≤ n-1, so i-j = 0, contradicting i ≠ j
    sorry
```

## Hints for the 3 sorry locations

**Sorry 1 (forward orthogonality):** You have `hcos_eq : cos(dθ) = cos(θ)` in context. Key Mathlib lemmas to look for: `Real.cos_eq_iff_of_lt_of_lt` or the characterization that cos(A) = cos(B) implies A = B + 2πk or A = -B + 2πk. Then use `hθ_period` and `hgcd` (gcd(1012,2025)=1) to get divisibility. You need to show `((i : ℤ) - (j : ℤ)).natAbs ∈ ({1, 2024} : Finset ℕ)`.

**Sorry 2 (backward orthogonality):** You need to show `cos(dθ) - cos(θ) = 0` for each case. For |d|=1: `Real.cos_neg` gives cos(-θ) = cos(θ). For |d|=2024: use `hθ_period` to show cos(2024θ) = cos(2025θ - θ) = cos(2π·1012 - θ) = cos(θ) via `Real.cos_sub_int_mul_two_pi` or similar.

**Sorry 3 (non-parallel):** You have `hcos_diff_one : cos((i-j)θ) = 1`. Use that cos(x) = 1 implies x is an integer multiple of 2π. Then the same gcd argument as Sorry 1 gives n | (i-j), contradicting |i-j| ≤ n-1.
