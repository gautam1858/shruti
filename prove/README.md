# prove: protocol contracts and the prover

`shruti prove <program> <contract>` symbolically executes the ISS with the data bytes and the bit period left symbolic and asks Z3 whether the contract can be violated. Output: a proof, or a counterexample pin trace. Contracts live beside the programs in `fw/`.
