import CslibFrontier.Crypto.Computational.OneWay.NP

namespace Theorems.«ccea3a5ebd7dad462c47»

def statement : Prop :=
  Cslib.Complexity.SAT ∉ Cslib.Complexity.P Cslib.Complexity.encCNF → ∃ f : List Bool → List Bool, Cslib.Crypto.OneWay f

end Theorems.«ccea3a5ebd7dad462c47»
