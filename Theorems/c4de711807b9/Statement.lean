import Mathlib

namespace Theorems.«c4de711807b9»

def statement : Prop :=
  ∀ ε : ℝ, 0 < ε → ∃ N : ℕ, ∀ n : ℕ, N ≤ n → 1 / ((n : ℝ) + 1) < ε

end Theorems.«c4de711807b9»
