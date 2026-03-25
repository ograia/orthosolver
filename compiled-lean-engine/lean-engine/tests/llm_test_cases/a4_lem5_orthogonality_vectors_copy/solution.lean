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
  -- Coprimality helper
  have hcop : IsCoprime (1012 : ℤ) (2025 : ℤ) :=
    (show Nat.Coprime 1012 2025 from hgcd).isCoprime_int
  constructor
  · -- Part 1: orthogonality iff |i-j| ∈ {1, 2024}
    intro i j hij
    rw [hdot_formula]
    constructor
    · -- Forward: dot = 0 → |i-j| ∈ {1,2024}
      intro hdot
      have hcos_eq : Real.cos ((↑(i : ℕ) - ↑(j : ℕ)) * θ) = Real.cos θ := by linarith
      set d : ℤ := (i : ℤ) - (j : ℤ) with hd_def
      have hd_real : (↑↑i - ↑↑j : ℝ) = (d : ℝ) := by push_cast; ring
      rw [hd_real] at hcos_eq
      have hd_bound : d.natAbs ≤ 2024 := by have := i.isLt; have := j.isLt; omega
      have hd_ne : d ≠ 0 := by intro h; exact hij (by ext; omega)
      rw [Real.cos_eq_cos_iff] at hcos_eq
      obtain ⟨k, hk | hk⟩ := hcos_eq
      · -- Case: θ = 2kπ + dθ → θ(1-d) = 2kπ → 1012(1-d) = 2025k
        have h_int_eq : (1012 : ℤ) * (1 - d) = 2025 * k := by
          have h1 : θ * (1 - (d : ℝ)) = 2 * ↑k * Real.pi := by linarith
          rw [hθ_def] at h1
          have h2 : ((1012 * (1 - d) : ℤ) : ℝ) = ((2025 * k : ℤ) : ℝ) := by
            push_cast; field_simp at h1; linarith
          exact_mod_cast h2
        have hdvd : (2025 : ℤ) ∣ (1 - d) :=
          hcop.symm.dvd_of_dvd_mul_right ⟨k, h_int_eq.symm⟩
        have hd_vals : d = 1 ∨ d = -2024 := by obtain ⟨m, hm⟩ := hdvd; omega
        simp only [Finset.mem_insert, Finset.mem_singleton]
        rcases hd_vals with rfl | rfl <;> simp [Int.natAbs]
      · -- Case: θ = 2kπ - dθ → θ(1+d) = 2kπ → 1012(1+d) = 2025k
        have h_int_eq : (1012 : ℤ) * (1 + d) = 2025 * k := by
          have h1 : θ * (1 + (d : ℝ)) = 2 * ↑k * Real.pi := by linarith
          rw [hθ_def] at h1
          have h2 : ((1012 * (1 + d) : ℤ) : ℝ) = ((2025 * k : ℤ) : ℝ) := by
            push_cast; field_simp at h1; linarith
          exact_mod_cast h2
        have hdvd : (2025 : ℤ) ∣ (1 + d) :=
          hcop.symm.dvd_of_dvd_mul_right ⟨k, h_int_eq.symm⟩
        have hd_vals : d = -1 ∨ d = 2024 := by obtain ⟨m, hm⟩ := hdvd; omega
        simp only [Finset.mem_insert, Finset.mem_singleton]
        rcases hd_vals with rfl | rfl <;> simp [Int.natAbs]
    · -- Backward: |i-j| ∈ {1,2024} → dot = 0
      intro hmem
      suffices h : Real.cos ((↑↑i - ↑↑j) * θ) = Real.cos θ by linarith
      simp only [Finset.mem_insert, Finset.mem_singleton] at hmem
      set d : ℤ := (i : ℤ) - (j : ℤ) with hd_def
      have hd_real : (↑↑i - ↑↑j : ℝ) = (d : ℝ) := by push_cast; ring
      rw [hd_real]
      rcases hmem with h1 | h2024
      · -- |d| = 1
        have : d = 1 ∨ d = -1 := by omega
        rcases this with rfl | rfl
        · simp
        · simp [Real.cos_neg]
      · -- |d| = 2024
        have : d = 2024 ∨ d = -2024 := by omega
        rcases this with rfl | rfl
        · have : (2024 : ℝ) * θ = -θ + ↑(1012 : ℤ) * (2 * Real.pi) := by
            push_cast; linarith [hθ_period]
          rw [this, Real.cos_add_int_mul_two_pi, Real.cos_neg]
        · have : (-2024 : ℝ) * θ = θ + ↑(-1012 : ℤ) * (2 * Real.pi) := by
            push_cast; linarith [hθ_period]
          rw [this, Real.cos_add_int_mul_two_pi]
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
    -- cos(dθ) = 1 → dθ = 2nπ → d*1012 = 2025*n → 2025 | d → d = 0 → contradiction
    rw [Real.cos_eq_one_iff] at hcos_diff_one
    obtain ⟨n, hn⟩ := hcos_diff_one
    have h1 : (↑↑i - ↑↑j) * θ = ↑n * (2 * Real.pi) := by linarith
    rw [hθ_def] at h1
    set d : ℤ := (i : ℤ) - (j : ℤ) with hd_def
    have h_int_eq : (1012 : ℤ) * d = 2025 * n := by
      have h2 : ((1012 * d : ℤ) : ℝ) = ((2025 * n : ℤ) : ℝ) := by
        push_cast; field_simp at h1; linarith
      exact_mod_cast h2
    have hdvd : (2025 : ℤ) ∣ d :=
      hcop.symm.dvd_of_dvd_mul_right ⟨n, h_int_eq.symm⟩
    have hd_bound : d.natAbs ≤ 2024 := by have := i.isLt; have := j.isLt; omega
    have hd_zero : d = 0 := by obtain ⟨m, hm⟩ := hdvd; omega
    exact hij (by ext; omega)
