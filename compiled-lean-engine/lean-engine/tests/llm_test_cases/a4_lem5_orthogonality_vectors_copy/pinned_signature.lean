theorem lem_20260315032701_8f502a27 :
    let θ : ℝ := 2 * Real.pi * 1012 / 2025
    let c : ℝ := Real.sqrt (-Real.cos θ)
    let v : Fin 2025 → Fin 3 → ℝ := fun i =>
      ![Real.cos (((i : ℕ) : ℝ) * θ), Real.sin (((i : ℕ) : ℝ) * θ), c]
    (∀ i j : Fin 2025, i ≠ j →
      (dotProduct (v i) (v j) = 0 ↔
        ((i : ℤ) - (j : ℤ)).natAbs ∈ ({1, 2024} : Finset ℕ))) ∧
    (∀ i j : Fin 2025, i ≠ j → ¬ ∃ t : ℝ, v i = t • v j)
