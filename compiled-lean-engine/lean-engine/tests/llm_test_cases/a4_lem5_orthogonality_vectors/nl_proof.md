# Natural Language Proof

**Lemma (L5):** Let n=2025, let m=1012, let θ = 2πm/n = π−π/2025, and let c=√(−cos θ). For each i∈{1,…,n}, define a vector v_i∈ℝ³ by v_i=(cos(iθ), sin(iθ), c). Then for any distinct i,j∈{1,…,n}, one has v_i·v_j=0 if and only if |i−j|∈{1, n−1}, and moreover v_i and v_j are not parallel.

**Proof:**

First, 0<π/2025<π/2, so cos(π/2025)>0. Because θ=π−π/2025, we have cos θ = −cos(π/2025)<0. Hence c=√(−cos θ)>0, so every v_i is nonzero.

Fix distinct i,j and write d=i−j. Then d≠0 and |d|≤n−1=2024. Using cos x cos y + sin x sin y = cos(x−y),
  v_i·v_j = cos(iθ)cos(jθ)+sin(iθ)sin(jθ)+c² = cos((i−j)θ)+c² = cos(dθ)−cos θ,
because c²=−cos θ. Therefore v_i·v_j=0 iff cos(dθ)=cos θ.

Assume v_i·v_j=0. Then cos(dθ)=cos θ. Since cos x − cos y = −2 sin((x+y)/2) sin((x−y)/2), the equality cos x=cos y is equivalent to x=2πt+y or x=2πt−y for some t∈ℤ. Applying this with x=dθ and y=θ, either dθ=2πt+θ or dθ=2πt−θ. As θ=2πm/n with m=1012 and n=2025, these become either m(d−1)=tn or m(d+1)=tn. Now gcd(m,n)=gcd(1012,2025)=1, because 2025=2·1012+1. So n divides d−1 or d+1; equivalently d≡1 or d≡−1 mod n.
Because −(n−1)≤d≤n−1 and d≠0, the congruence d≡1 mod n gives d=1 or d=−(n−1), while d≡−1 mod n gives d=−1 or d=n−1. Hence |d|∈{1,n−1}, i.e. |i−j|∈{1,n−1}.

Conversely, if |i−j|=1, then d=±1, so cos(dθ)=cos θ and thus v_i·v_j=0. If |i−j|=n−1, then d=±(n−1). Since nθ=2πm, we get
  cos((n−1)θ)=cos(nθ−θ)=cos(2πm−θ)=cos θ,
and also cos(−(n−1)θ)=cos((n−1)θ). Thus again v_i·v_j=0. Therefore, for distinct i,j,
  v_i·v_j=0 iff |i−j|∈{1,n−1}.

Now suppose distinct v_i and v_j were parallel. Then v_i=λv_j for some real λ. Comparing third coordinates gives c=λc. Since c>0, λ=1, so actually v_i=v_j. Thus cos(iθ)=cos(jθ) and sin(iθ)=sin(jθ). Therefore
  cos((i−j)θ)=cos(iθ)cos(jθ)+sin(iθ)sin(jθ)=1.
For real x, cos x=1 iff x=2πt for some t∈ℤ. Hence (i−j)θ=2πt for some t∈ℤ. Substituting θ=2πm/n gives (i−j)m=tn. Again gcd(m,n)=1, so n divides i−j. But |i−j|≤n−1, hence i−j=0, so i=j, contradicting distinctness. Therefore no two distinct vectors v_i and v_j are parallel.
