# Failure Analysis

## Problem: Putnam 2024 A4, Lemma 5 (Orthogonality Vectors in R³)

## Why Claude Failed (6 attempts, best = 3 sorries)

Claude successfully proved:
- cos θ < 0, c > 0
- The dot product formula: v_i · v_j = cos((i-j)θ) - cos θ
- The non-parallel direction (Part 2) up to the final contradiction step

### Remaining 3 sorry locations:

**Sorry 1 (line 68): Forward direction of orthogonality**
Given cos(dθ) = cos(θ), prove |i-j| ∈ {1, 2024}.
- Requires: cos(A) = cos(B) → A = B + 2πt or A = -B + 2πt
- Then substituting θ = 2πm/n: m(d-1) = tn or m(d+1) = tn
- gcd(1012, 2025) = 1 (proved via native_decide)
- So n | (d-1) or n | (d+1), and |d| ≤ 2024, d ≠ 0 → |d| ∈ {1, 2024}

**Sorry 2 (line 71): Backward direction**
Given |i-j| ∈ {1, 2024}, prove cos(dθ) - cos(θ) = 0.
- For d = ±1: cos(±θ) = cos(θ) (even function)
- For d = ±2024: cos(2024θ) = cos(2025θ - θ) = cos(2πm·1012 - θ) = cos(-θ) = cos(θ)

**Sorry 3 (line 92): Non-parallel contradiction**
Given cos((i-j)θ) = 1, derive i = j (contradiction).
- cos(x) = 1 ↔ x = 2πt for some t ∈ ℤ
- So (i-j)θ = 2πt, substituting θ gives (i-j)m = tn
- gcd(m,n) = 1, so n | (i-j), but |i-j| ≤ n-1, so i = j

## Key Difficulty
The core challenge is the number-theoretic argument: going from a trigonometric identity
(cos equality) to an integer divisibility statement via gcd(1012, 2025) = 1. This requires
chaining together:
1. Real analysis (cos equality characterization)
2. Algebraic manipulation (substituting θ = 2πm/n)
3. Number theory (coprimality → divisibility)
4. Finite arithmetic (bounding |d| to get exact values)

Lean's Mathlib has all the pieces but finding and connecting them is non-trivial.
Specific missing pieces Claude struggled with:
- `Real.cos_eq_iff_of_lt_of_lt` or equivalent characterization of cos equality
- Converting between real-valued equations and integer divisibility
- `Nat.Coprime.dvd_of_dvd_mul_left` or similar for the gcd argument
