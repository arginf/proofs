import CslibFrontier.Crypto.Computational.OneWayToPRG

namespace Theorems.«c72a0c096b0abaae6598»

def statement : Prop :=
  ∀ f : List Bool → List Bool, Cslib.Crypto.OneWay f → ∃ g : List Bool → List Bool, Cslib.Crypto.PseudorandomGenerator g (fun n => n + 1)

end Theorems.«c72a0c096b0abaae6598»
