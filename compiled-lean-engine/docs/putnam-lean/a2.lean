import Mathlib

open scoped BigOperators

/- ===== Statements ===== -/
open Set Filter

-- Root: Find the largest a and smallest b such that a·x(π-x) ≤ sin x ≤ b·x(π-x) for all x ∈ [0,π].
-- Answer: a = 1/π, b = 4/π².
-- L1: The sandwich inequality on [0,π] is equivalent to bounding the ratio on (0,π).
-- L2: f(x) = sin x / (x(π-x)) is symmetric about π/2, i.e. f(π-x) = f(x).
-- L3: lim_{x→0+} f(x) = 1/π and lim_{x→π-} f(x) = 1/π.
-- L4: f(x) ≥ 1/π for all x ∈ (0,π). Equivalently, sin x ≥ x(π-x)/π.
-- L5: f(x) ≤ 4/π² for all x ∈ (0,π), with equality iff x = π/2.
-- L6: Given bounds and limits, inf f = 1/π and sup f = 4/π² on (0,π).
-- L7: If inf f = α and sup f = β (finite, bounded), then α is the largest lower bound
-- and β is the smallest upper bound.

/- ===== Lemmas ===== -/
open Real


theorem lem_20260315031457_d3b391e8 (x : ℝ) (hx : x ∈ Ioo 0 Real.pi) : Real.sin (Real.pi - x) / ((Real.pi - x) * x) = Real.sin x / (x * (Real.pi - x)) := by
  rw [Real.sin_pi_sub, mul_comm (Real.pi - x) x]

theorem lem_20260315032055_4bc9cc21 (f : ℝ → ℝ) (α β : ℝ) (h_nonempty : (image f (Ioo 0 Real.pi)).Nonempty) (h_bdd_below : BddBelow (image f (Ioo 0 Real.pi))) (h_bdd_above : BddAbove (image f (Ioo 0 Real.pi))) (h_inf : sInf (image f (Ioo 0 Real.pi)) = α) (h_sup : sSup (image f (Ioo 0 Real.pi)) = β) : (∀ x : ℝ, x ∈ Ioo 0 Real.pi → α ≤ f x) ∧ (∀ A : ℝ, (∀ x : ℝ, x ∈ Ioo 0 Real.pi → A ≤ f x) → A ≤ α) ∧ (∀ x : ℝ, x ∈ Ioo 0 Real.pi → f x ≤ β) ∧ (∀ B : ℝ, (∀ x : ℝ, x ∈ Ioo 0 Real.pi → f x ≤ B) → β ≤ B) := by
  refine ⟨?_, ?_, ?_, ?_⟩
  · -- α ≤ f x for all x in (0, π): follows from sInf being a lower bound
    intro x hx
    rw [← h_inf]
    exact csInf_le h_bdd_below (mem_image_of_mem f hx)
  · -- A ≤ α for any lower bound A: follows from sInf being the greatest lower bound
    intro A hA
    rw [← h_inf]
    exact le_csInf h_nonempty (fun b ⟨x, hx, hxb⟩ => hxb ▸ hA x hx)
  · -- f x ≤ β for all x in (0, π): follows from sSup being an upper bound
    intro x hx
    rw [← h_sup]
    exact le_csSup h_bdd_above (mem_image_of_mem f hx)
  · -- β ≤ B for any upper bound B: follows from sSup being the least upper bound
    intro B hB
    rw [← h_sup]
    exact csSup_le h_nonempty (fun b ⟨x, hx, hxb⟩ => hxb ▸ hB x hx)

theorem lem_20260315031430_1912f371 (a b : ℝ) : (∀ x : ℝ, x ∈ Icc 0 Real.pi → a * (x * (Real.pi - x)) ≤ Real.sin x ∧ Real.sin x ≤ b * (x * (Real.pi - x))) ↔ (∀ x : ℝ, x ∈ Ioo 0 Real.pi → a ≤ Real.sin x / (x * (Real.pi - x)) ∧ Real.sin x / (x * (Real.pi - x)) ≤ b) := by
  constructor
  · intro h x hx
    have hx_icc : x ∈ Icc 0 Real.pi := Ioo_subset_Icc_self hx
    have hx_pos : (0 : ℝ) < x := hx.1
    have hpi_x_pos : (0 : ℝ) < Real.pi - x := sub_pos.mpr hx.2
    have hD_pos : (0 : ℝ) < x * (Real.pi - x) := mul_pos hx_pos hpi_x_pos
    obtain ⟨h1, h2⟩ := h x hx_icc
    exact ⟨(le_div_iff₀ hD_pos).mpr h1, (div_le_iff₀ hD_pos).mpr h2⟩
  · intro h x hx
    by_cases hx0 : x = 0
    · subst hx0; simp [Real.sin_zero]
    · by_cases hxpi : x = Real.pi
      · subst hxpi; simp [Real.sin_pi]
      · have hx_pos : (0 : ℝ) < x := lt_of_le_of_ne hx.1 (Ne.symm hx0)
        have hxlt : x < Real.pi := lt_of_le_of_ne hx.2 hxpi
        have hpi_x_pos : (0 : ℝ) < Real.pi - x := sub_pos.mpr hxlt
        have hD_pos : (0 : ℝ) < x * (Real.pi - x) := mul_pos hx_pos hpi_x_pos
        have hx_ioo : x ∈ Ioo 0 Real.pi := ⟨hx_pos, hxlt⟩
        obtain ⟨h1, h2⟩ := h x hx_ioo
        exact ⟨(le_div_iff₀ hD_pos).mp h1, (div_le_iff₀ hD_pos).mp h2⟩

theorem lem_20260315032024_9b4e1f0b (h_lower : ∀ x : ℝ, x ∈ Ioo 0 Real.pi → 1 / Real.pi ≤ Real.sin x / (x * (Real.pi - x))) (h_lim_0 : Tendsto (fun x => Real.sin x / (x * (Real.pi - x))) (nhdsWithin 0 (Ioi 0)) (nhds (1 / Real.pi))) (h_lim_pi : Tendsto (fun x => Real.sin x / (x * (Real.pi - x))) (nhdsWithin Real.pi (Iio Real.pi)) (nhds (1 / Real.pi))) (h_upper : ∀ x : ℝ, x ∈ Ioo 0 Real.pi → Real.sin x / (x * (Real.pi - x)) ≤ 4 / Real.pi ^ 2) (h_eq : Real.sin (Real.pi / 2) / (Real.pi / 2 * (Real.pi - Real.pi / 2)) = 4 / Real.pi ^ 2) : sInf (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) = 1 / Real.pi ∧ sSup (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) = 4 / Real.pi ^ 2 := by
  have hpi_pos : (0 : ℝ) < Real.pi := Real.pi_pos
  have hpi2_mem : Real.pi / 2 ∈ Ioo (0 : ℝ) Real.pi := ⟨by linarith, by linarith⟩
  have hne : (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)).Nonempty :=
    ⟨_, mem_image_of_mem _ hpi2_mem⟩
  have hbdd_below : BddBelow (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) := by
    refine ⟨1 / Real.pi, ?_⟩
    rintro _ ⟨x, hx, rfl⟩
    exact h_lower x hx
  have hbdd_above : BddAbove (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) := by
    refine ⟨4 / Real.pi ^ 2, ?_⟩
    rintro _ ⟨x, hx, rfl⟩
    exact h_upper x hx
  constructor
  · -- sInf = 1/π
    apply le_antisymm
    · -- sInf ≤ 1/π: by contradiction using limit at 0+
      by_contra hlt
      push_neg at hlt
      have h1 : ∀ᶠ x in nhdsWithin (0 : ℝ) (Ioi 0),
          Real.sin x / (x * (Real.pi - x)) <
            sInf (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) :=
        h_lim_0.eventually (Iio_mem_nhds hlt)
      have h2 : ∀ᶠ x in nhdsWithin (0 : ℝ) (Ioi 0), x ∈ Ioo (0 : ℝ) Real.pi := by
        filter_upwards [self_mem_nhdsWithin, nhdsWithin_le_nhds (Iio_mem_nhds hpi_pos)]
        exact fun x hpos hlt_pi => ⟨hpos, hlt_pi⟩
      obtain ⟨x₀, hgx₀, hx₀_mem⟩ := (h1.and h2).exists
      have hsInf_le := csInf_le hbdd_below (mem_image_of_mem _ hx₀_mem)
      linarith
    · -- 1/π ≤ sInf: 1/π is a lower bound
      apply le_csInf hne
      rintro _ ⟨x, hx, rfl⟩
      exact h_lower x hx
  · -- sSup = 4/π²
    apply le_antisymm
    · -- sSup ≤ 4/π²: upper bound
      apply csSup_le hne
      rintro _ ⟨x, hx, rfl⟩
      exact h_upper x hx
    · -- 4/π² ≤ sSup: achieved at x = π/2
      have hmem : Real.sin (Real.pi / 2) / (Real.pi / 2 * (Real.pi - Real.pi / 2)) ∈
          image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi) :=
        mem_image_of_mem _ hpi2_mem
      calc 4 / Real.pi ^ 2
          = Real.sin (Real.pi / 2) / (Real.pi / 2 * (Real.pi - Real.pi / 2)) := h_eq.symm
        _ ≤ sSup (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) :=
            le_csSup hbdd_above hmem

theorem lem_20260315031613_1645b79b : (∀ x : ℝ, x ∈ Ioo 0 Real.pi → Real.sin x / (x * (Real.pi - x)) ≤ 4 / Real.pi ^ 2) ∧ (Real.sin (Real.pi / 2) / (Real.pi / 2 * (Real.pi - Real.pi / 2)) = 4 / Real.pi ^ 2) ∧ (∀ x : ℝ, x ∈ Ioo 0 Real.pi → Real.sin x / (x * (Real.pi - x)) = 4 / Real.pi ^ 2 → x = Real.pi / 2) := by
  have hpi : (0 : ℝ) < π := pi_pos
  have hpi_ne : π ≠ 0 := ne_of_gt hpi
  have hpi2 : (0 : ℝ) < π ^ 2 := by positivity
  have hsc := strictConcaveOn_sin_Icc
  have cos_half (t : ℝ) : cos t = 1 - 2 * sin (t / 2) ^ 2 := by
    have h1 := cos_add (t / 2) (t / 2)
    have h2 := sin_sq_add_cos_sq (t / 2)
    have : t / 2 + t / 2 = t := by ring
    rw [this] at h1
    nlinarith [sq (sin (t / 2)), sq (cos (t / 2))]
  have hsq2 : Real.sqrt 2 ^ 2 = 2 := Real.sq_sqrt (by norm_num : (0 : ℝ) ≤ 2)
  have hsqrt2_pos : (0 : ℝ) < Real.sqrt 2 := Real.sqrt_pos.mpr (by norm_num : (0 : ℝ) < 2)
  -- Step 1: sin(u) > (2√2/π)u for u ∈ (0, π/4)
  have sin_strict : ∀ u : ℝ, 0 < u → u < π / 4 → sin u > 2 * Real.sqrt 2 / π * u := by
    intro u hu0 hu4
    have h0m : (0 : ℝ) ∈ Icc 0 π := ⟨le_refl _, le_of_lt hpi⟩
    have h4m : π / 4 ∈ Icc (0 : ℝ) π := ⟨by linarith, by linarith⟩
    have hne : (0 : ℝ) ≠ π / 4 := by linarith
    have ha : (0 : ℝ) < 1 - 4 * u / π := by rw [sub_pos, div_lt_one hpi]; linarith
    have hb : (0 : ℝ) < 4 * u / π := by positivity
    have hab : 1 - 4 * u / π + 4 * u / π = 1 := by ring
    have key := hsc.2 h0m h4m hne ha hb hab
    simp only [smul_eq_mul, sin_zero, mul_zero, zero_add] at key
    have hpt : 4 * u / π * (π / 4) = u := by field_simp
    rw [hpt] at key
    rw [sin_pi_div_four] at key
    -- key : 4 * u / π * (√2 / 2) < sin u
    have hkey_eq : 4 * u / π * (Real.sqrt 2 / 2) = 2 * Real.sqrt 2 / π * u := by
      field_simp; ring
    linarith
  -- Step 2: sin(u) ≥ (2√2/π)u for u ∈ [0, π/4]
  have sin_lb : ∀ u : ℝ, 0 ≤ u → u ≤ π / 4 → sin u ≥ 2 * Real.sqrt 2 / π * u := by
    intro u hu0 hu4
    rcases eq_or_lt_of_le hu0 with rfl | hu_pos
    · simp
    rcases eq_or_lt_of_le hu4 with rfl | hu_lt
    · rw [sin_pi_div_four]
      have : 2 * Real.sqrt 2 / π * (π / 4) = Real.sqrt 2 / 2 := by field_simp; ring
      linarith
    · exact le_of_lt (sin_strict u hu_pos hu_lt)
  -- Step 3: cos(t) ≤ 1 - 4/π² · t² for t ∈ [0, π/2]
  have cos_ub : ∀ t : ℝ, 0 ≤ t → t ≤ π / 2 → cos t ≤ 1 - 4 / π ^ 2 * t ^ 2 := by
    intro t ht0 ht2
    rw [cos_half]
    have ht2_nn : 0 ≤ t / 2 := by linarith
    have ht2_le : t / 2 ≤ π / 4 := by linarith
    have hslb := sin_lb (t / 2) ht2_nn ht2_le
    have hsin_nn := sin_nonneg_of_nonneg_of_le_pi ht2_nn (by linarith : t / 2 ≤ π)
    have hslb_neg : -(sin (t / 2)) ≤ 2 * Real.sqrt 2 / π * (t / 2) := by
      have : 0 ≤ 2 * Real.sqrt 2 / π * (t / 2) := by positivity
      linarith
    have hsq_lb := sq_le_sq' hslb_neg hslb
    have hcompute : (2 * Real.sqrt 2 / π * (t / 2)) ^ 2 = 2 * t ^ 2 / π ^ 2 := by
      field_simp; nlinarith [hsq2]
    have h_eq : 4 / π ^ 2 * t ^ 2 = 2 * (2 * t ^ 2 / π ^ 2) := by field_simp; ring
    have h1 : 2 * t ^ 2 / π ^ 2 ≤ sin (t / 2) ^ 2 := by linarith [hsq_lb, hcompute]
    linarith
  -- Step 4: sin(x) ≤ (4/π²)x(π-x) for x ∈ (0, π)
  have sin_ub : ∀ x : ℝ, x ∈ Ioo 0 π → sin x ≤ 4 / π ^ 2 * (x * (π - x)) := by
    intro x ⟨hx0, hxp⟩
    have habs_nn : 0 ≤ |π / 2 - x| := abs_nonneg _
    have habs_le : |π / 2 - x| ≤ π / 2 := by rw [abs_le]; constructor <;> linarith
    have hsincos : sin x = cos (π / 2 - x) := by rw [cos_pi_div_two_sub]
    have hce : cos (π / 2 - x) = cos |π / 2 - x| := by
      rcases le_or_gt x (π / 2) with h | h
      · rw [abs_of_nonneg (by linarith)]
      · rw [abs_of_neg (by linarith), cos_neg]
    rw [hsincos, hce]
    calc cos |π / 2 - x|
        ≤ 1 - 4 / π ^ 2 * |π / 2 - x| ^ 2 := cos_ub _ habs_nn habs_le
      _ = 4 / π ^ 2 * (x * (π - x)) := by rw [sq_abs]; field_simp; ring
  -- Step 5: Equality implies x = π/2
  have sin_eq : ∀ x : ℝ, x ∈ Ioo 0 π → sin x = 4 / π ^ 2 * (x * (π - x)) → x = π / 2 := by
    intro x hx heq
    by_contra hne
    set t := |π / 2 - x| with ht_def
    have ht_pos : 0 < t := by rw [ht_def, abs_pos]; intro h; exact hne (by linarith)
    have ht_lt : t < π / 2 := by rw [ht_def, abs_lt]; constructor <;> linarith [hx.1, hx.2]
    have ht2_pos : 0 < t / 2 := by linarith
    have ht2_lt : t / 2 < π / 4 := by linarith
    have hstr := sin_strict (t / 2) ht2_pos ht2_lt
    have hsc2 : sin x = cos t := by
      rw [ht_def, show sin x = cos (π / 2 - x) from by rw [cos_pi_div_two_sub]]
      rcases le_or_gt x (π / 2) with h | h
      · rw [abs_of_nonneg (by linarith)]
      · rw [abs_of_neg (by linarith), cos_neg]
    have hcos_val : cos t = 1 - 4 / π ^ 2 * t ^ 2 := by
      rw [← hsc2, heq, ht_def, sq_abs]; field_simp; ring
    rw [cos_half] at hcos_val
    have h_2sin : 2 * sin (t / 2) ^ 2 = 4 / π ^ 2 * t ^ 2 := by linarith
    have h_bridge : 4 / π ^ 2 * t ^ 2 = 2 * (2 * t ^ 2 / π ^ 2) := by field_simp; ring
    have hsin_sq_eq : sin (t / 2) ^ 2 = 2 * t ^ 2 / π ^ 2 := by linarith
    have hnn : 0 ≤ sin (t / 2) := sin_nonneg_of_nonneg_of_le_pi (by linarith) (by linarith)
    have hrhs_pos : 0 < 2 * Real.sqrt 2 / π * (t / 2) := by positivity
    have hsq_strict : (2 * Real.sqrt 2 / π * (t / 2)) ^ 2 < sin (t / 2) ^ 2 :=
      sq_lt_sq' (by linarith) hstr
    have hcompute2 : (2 * Real.sqrt 2 / π * (t / 2)) ^ 2 = 2 * t ^ 2 / π ^ 2 := by
      field_simp; nlinarith [hsq2]
    linarith
  -- Combine the three parts
  refine ⟨fun x hx => ?_, ?_, fun x hx heq => ?_⟩
  · -- Part 1: sin(x)/(x(π-x)) ≤ 4/π²
    have hprod : 0 < x * (π - x) := mul_pos hx.1 (by linarith [hx.2])
    rw [div_le_iff₀ hprod]
    exact sin_ub x hx
  · -- Part 2: sin(π/2)/((π/2)(π-π/2)) = 4/π²
    rw [show π - π / 2 = π / 2 from by ring, sin_pi_div_two]
    have : π / 2 ≠ 0 := by positivity
    field_simp
    ring
  · -- Part 3: equality → x = π/2
    have hprod : 0 < x * (π - x) := mul_pos hx.1 (by linarith [hx.2])
    have heq' : sin x = 4 / π ^ 2 * (x * (π - x)) := by
      rwa [div_eq_iff (ne_of_gt hprod)] at heq
    exact sin_eq x hx heq'

theorem lem_20260315031522_8bfa0dc7 : Tendsto (fun x => Real.sin x / (x * (Real.pi - x))) (nhdsWithin 0 (Ioi 0)) (nhds (1 / Real.pi)) ∧ Tendsto (fun x => Real.sin x / (x * (Real.pi - x))) (nhdsWithin Real.pi (Iio Real.pi)) (nhds (1 / Real.pi)) := by
  have hπ_pos : (0 : ℝ) < Real.pi := Real.pi_pos
  have hπ_ne : (Real.pi : ℝ) ≠ 0 := ne_of_gt hπ_pos
  -- sin(x)/x → 1 as x → 0+ via sinc continuity at 0
  have hsinx : Tendsto (fun x => Real.sin x / x) (nhdsWithin 0 (Ioi 0)) (nhds 1) := by
    have hcont : Tendsto Real.sinc (nhds 0) (nhds 1) := by
      have := Real.continuous_sinc.continuousAt (x := (0 : ℝ))
      rwa [ContinuousAt, Real.sinc_zero] at this
    refine (hcont.mono_left nhdsWithin_le_nhds).congr' ?_
    filter_upwards [self_mem_nhdsWithin] with x (hx : (0 : ℝ) < x)
    show Real.sinc x = Real.sin x / x
    have hne : x ≠ 0 := hx.ne'
    simp [Real.sinc, hne]
  -- Part 1: sin(x)/(x(π-x)) = (sin(x)/x) / (π-x) → 1/π as x→0+
  have part1 : Tendsto (fun x => Real.sin x / (x * (Real.pi - x)))
      (nhdsWithin 0 (Ioi 0)) (nhds (1 / Real.pi)) := by
    have hpx : Tendsto (fun x : ℝ => Real.pi - x) (nhdsWithin 0 (Ioi 0)) (nhds Real.pi) := by
      have h : Tendsto (fun x : ℝ => Real.pi - x) (nhds 0) (nhds (Real.pi - 0)) :=
        tendsto_const_nhds.sub tendsto_id
      rw [sub_zero] at h
      exact h.mono_left nhdsWithin_le_nhds
    exact (hsinx.div hpx hπ_ne).congr'
      (by filter_upwards with x; exact div_div _ _ _)
  constructor
  · exact part1
  · -- Part 2: limit at π- via substitution y = π - x
    have hmap : Tendsto (fun x : ℝ => Real.pi - x)
        (nhdsWithin Real.pi (Iio Real.pi)) (nhdsWithin 0 (Ioi 0)) := by
      apply tendsto_nhdsWithin_of_tendsto_nhds_of_eventually_within
      · have h : Tendsto (fun x : ℝ => Real.pi - x) (nhds Real.pi) (nhds (Real.pi - Real.pi)) :=
          tendsto_const_nhds.sub tendsto_id
        rw [sub_self] at h
        exact h.mono_left nhdsWithin_le_nhds
      · filter_upwards [self_mem_nhdsWithin] with x hx
        exact sub_pos.mpr (mem_Iio.mp hx)
    exact (part1.comp hmap).congr' (by
      filter_upwards with x
      simp only [Function.comp_apply, Real.sin_pi_sub]
      congr 1; ring)

theorem lem_20260315031549_f1fcee00 (x : ℝ) (hx : x ∈ Ioo 0 Real.pi) :
    1 / Real.pi ≤ Real.sin x / (x * (Real.pi - x)) := by
  have hπ := pi_pos
  have hx0 := hx.1
  have hxπ := hx.2
  have hπx : (0 : ℝ) < π - x := sub_pos.mpr hxπ
  have hD : (0 : ℝ) < x * (π - x) := mul_pos hx0 hπx
  rw [div_le_div_iff₀ hπ hD, one_mul]
  -- Goal: x * (π - x) ≤ sin x * π
  -- Reduce to y ∈ (0, π/2] by symmetry sin(π-x) = sin(x)
  suffices key : ∀ y : ℝ, 0 < y → y ≤ π / 2 → y * (π - y) ≤ sin y * π by
    by_cases hle : x ≤ π / 2
    · exact key x hx0 hle
    · push_neg at hle
      rw [show sin x = sin (π - x) from (sin_pi_sub x).symm,
          show x * (π - x) = (π - x) * (π - (π - x)) from by ring]
      exact key (π - x) hπx (by linarith)
  intro y hy0 hyπ2
  -- Step 1: sin is monotone on [0, π/2] (since cos > 0 on (-π/2, π/2))
  have sin_mono : MonotoneOn sin (Icc 0 (π / 2)) :=
    monotoneOn_of_deriv_nonneg (convex_Icc 0 (π / 2))
      continuous_sin.continuousOn
      (fun z _ => (hasDerivAt_sin z).differentiableAt.differentiableWithinAt)
      (fun z hz => by
        simp only [interior_Icc] at hz
        rw [(hasDerivAt_sin z).deriv]
        exact le_of_lt (cos_pos_of_mem_Ioo ⟨by linarith [hz.1], hz.2⟩))
  -- Step 2: cos is concave on [0, π/2] (since cos' = -sin is antitone)
  have hcos_conc : ConcaveOn ℝ (Icc 0 (π / 2)) cos :=
    AntitoneOn.concaveOn_of_deriv (convex_Icc 0 (π / 2))
      continuous_cos.continuousOn
      (fun z _ => (hasDerivAt_cos z).differentiableAt.differentiableWithinAt)
      (fun u hu v hv huv => by
        simp only [interior_Icc] at hu hv
        rw [(hasDerivAt_cos u).deriv, (hasDerivAt_cos v).deriv]
        -- Need: -sin v ≤ -sin u, i.e., sin u ≤ sin v
        linarith [sin_mono ⟨le_of_lt hu.1, le_of_lt hu.2⟩ ⟨le_of_lt hv.1, le_of_lt hv.2⟩ huv])
  -- Step 3: cos(t) ≥ 1 - 2t/π for t ∈ [0, π/2] (chord bound from concavity)
  have cos_lb : ∀ t : ℝ, 0 ≤ t → t ≤ π / 2 → 1 - 2 * t / π ≤ cos t := by
    intro t ht0 htπ2
    have ha : 0 ≤ 1 - 2 * t / π := by rw [sub_nonneg, div_le_one hπ]; linarith
    have hb : 0 ≤ 2 * t / π := by positivity
    have hab : (1 - 2 * t / π) + 2 * t / π = 1 := by ring
    have ht_eq : (1 - 2 * t / π) • (0 : ℝ) + (2 * t / π) • (π / 2) = t := by
      simp only [smul_eq_mul]; field_simp; ring
    have h := hcos_conc.2 (left_mem_Icc.mpr (by linarith : (0 : ℝ) ≤ π / 2))
      (right_mem_Icc.mpr (by linarith : (0 : ℝ) ≤ π / 2)) ha hb hab
    rw [ht_eq, cos_zero, cos_pi_div_two] at h
    simp only [smul_eq_mul, mul_one, mul_zero, add_zero] at h
    exact h
  -- Step 4: F(y) = sin(y)*π - y*(π-y) is monotone on [0, π/2] (F' ≥ 0)
  have hF_mono : MonotoneOn (fun y => sin y * π - y * (π - y)) (Icc 0 (π / 2)) :=
    monotoneOn_of_deriv_nonneg (convex_Icc 0 (π / 2))
      ((continuous_sin.mul continuous_const).sub
        (continuous_id.mul (continuous_const.sub continuous_id))).continuousOn
      (fun z _ => by
        apply DifferentiableAt.differentiableWithinAt
        exact (((hasDerivAt_sin z).differentiableAt).mul (differentiableAt_const π)).sub
          (differentiableAt_fun_id.mul ((differentiableAt_const π).sub differentiableAt_fun_id)))
      (fun t ht => by
        simp only [interior_Icc] at ht
        have hd : HasDerivAt (fun y => sin y * π - y * (π - y))
            (cos t * π - (1 * (π - t) + t * (0 - 1))) t :=
          ((hasDerivAt_sin t).mul_const π).sub
            ((hasDerivAt_id t).mul ((hasDerivAt_const t π).sub (hasDerivAt_id t)))
        rw [hd.deriv]
        have hcl := cos_lb t (le_of_lt ht.1) (le_of_lt ht.2)
        have hmul : (1 - 2 * t / π) * π ≤ cos t * π :=
          mul_le_mul_of_nonneg_right hcl (le_of_lt hπ)
        have hsimp : (1 - 2 * t / π) * π = π - 2 * t := by field_simp
        linarith)
  -- Step 5: F(0) = 0, F monotone → F(y) ≥ 0 → y*(π-y) ≤ sin(y)*π
  have h0m : (0 : ℝ) ∈ Icc 0 (π / 2) := left_mem_Icc.mpr (by linarith)
  have hym : y ∈ Icc (0 : ℝ) (π / 2) := ⟨le_of_lt hy0, hyπ2⟩
  have := hF_mono h0m hym (le_of_lt hy0)
  simp only [sin_zero, zero_mul, sub_self] at this
  linarith


/- ===== Root ===== -/
open Set Filter

theorem root_prob_20260315030804_a862b834 : (∀ x : ℝ, x ∈ Icc 0 Real.pi → (1 / Real.pi) * (x * (Real.pi - x)) ≤ Real.sin x ∧ Real.sin x ≤ (4 / Real.pi ^ 2) * (x * (Real.pi - x))) ∧ (∀ a : ℝ, (∀ x : ℝ, x ∈ Icc 0 Real.pi → a * (x * (Real.pi - x)) ≤ Real.sin x) → a ≤ 1 / Real.pi) ∧ (∀ b : ℝ, (∀ x : ℝ, x ∈ Icc 0 Real.pi → Real.sin x ≤ b * (x * (Real.pi - x))) → 4 / Real.pi ^ 2 ≤ b) := by
  -- Unpack lemma components
  obtain ⟨hLim0, hLimPi⟩ := lem_20260315031522_8bfa0dc7
  obtain ⟨hUB, hEq, _⟩ := lem_20260315031613_1645b79b
  -- Compute inf and sup of f on (0, π)
  obtain ⟨hInf, hSup⟩ := lem_20260315032024_9b4e1f0b
    lem_20260315031549_f1fcee00 hLim0 hLimPi hUB hEq
  -- Nonemptiness and boundedness of the image set
  have hpi_pos := Real.pi_pos
  have hpi2_mem : Real.pi / 2 ∈ Ioo (0 : ℝ) Real.pi := ⟨by linarith, by linarith⟩
  have hne : (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)).Nonempty :=
    ⟨_, mem_image_of_mem _ hpi2_mem⟩
  have hbdd_below : BddBelow (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) :=
    ⟨1 / Real.pi, fun _ ⟨x, hx, he⟩ => he ▸ lem_20260315031549_f1fcee00 x hx⟩
  have hbdd_above : BddAbove (image (fun x => Real.sin x / (x * (Real.pi - x))) (Ioo 0 Real.pi)) :=
    ⟨4 / Real.pi ^ 2, fun _ ⟨x, hx, he⟩ => he ▸ hUB x hx⟩
  -- Optimality properties from inf/sup
  obtain ⟨hLB, hOptLB, hUB', hOptUB⟩ := lem_20260315032055_4bc9cc21
    (fun x => Real.sin x / (x * (Real.pi - x)))
    (1 / Real.pi) (4 / Real.pi ^ 2)
    hne hbdd_below hbdd_above hInf hSup
  refine ⟨?_, ?_, ?_⟩
  · -- Part 1: The bounds (1/π)·x(π-x) ≤ sin x ≤ (4/π²)·x(π-x) hold on [0, π]
    exact (lem_20260315031430_1912f371 (1 / Real.pi) (4 / Real.pi ^ 2)).mpr
      (fun x hx => ⟨hLB x hx, hUB' x hx⟩)
  · -- Part 2: a ≤ 1/π (1/π is the largest admissible lower-bound constant)
    intro a ha
    apply hOptLB
    intro x hx
    have hD_pos : (0 : ℝ) < x * (Real.pi - x) := mul_pos hx.1 (sub_pos.mpr hx.2)
    exact (le_div_iff₀ hD_pos).mpr (ha x (Ioo_subset_Icc_self hx))
  · -- Part 3: 4/π² ≤ b (4/π² is the smallest admissible upper-bound constant)
    intro b hb
    apply hOptUB
    intro x hx
    have hD_pos : (0 : ℝ) < x * (Real.pi - x) := mul_pos hx.1 (sub_pos.mpr hx.2)
    exact (div_le_iff₀ hD_pos).mpr (hb x (Ioo_subset_Icc_self hx))
