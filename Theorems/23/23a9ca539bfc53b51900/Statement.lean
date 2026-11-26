import CslibFrontier.Crypto.Computational.OneWay.NP

namespace Theorems.«23a9ca539bfc53b51900»

def statement : Prop :=
  ∀ f : List Bool → List Bool, Cslib.Crypto.OneWay f → Cslib.Complexity.SAT ∉ Cslib.Complexity.P Cslib.Complexity.encCNF

end Theorems.«23a9ca539bfc53b51900»
