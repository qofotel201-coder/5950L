# Production mesh region review

Status: **PASS**. The coarse partition is frozen and the medium mesh is authorized but not built.

| Region | Coarse size (m) | Medium size (m) | Transition (m) |
|---|---:|---:|---:|
| farfield | 0.054221688713527318 | 0.043052020838540692 | 0.5 |
| transition | 0.036147792475684878 | 0.028701347225693796 | 0.5 |
| near_body_core | 0.024098528317123252 | 0.019134231483795863 | 0.29999999999999999 |
| internal_passage | 0.016567738218022234 | 0.013154784145109654 | 0.20000000000000001 |

The size order is strictly farfield > transition > near-body > internal passage. Bounds are nested, transitions are linear and positive, and the volume callback composes overlaps by minimum size. The frozen wall retains 60 layers, 0.4 m tangential triangulation, first-layer height, growth ratio, and Pilot repair parameters.

AutoDL read-only measurement of the accepted coarse MSH also passed. Tet4 characteristic-length medians are `0.0368541`, `0.0247716`, `0.0164372`, and `0.0117414 m` in that same order. The 280,320 Prism6 cells have a wall-normal thickness median of `2.76986e-7 m`, which proves wall-normal refinement without changing the frozen tangential surface triangulation. The measured MSH SHA-256 remains `68f6af4cec625c62ef60fe2a34ccdb5ca09630240c1b8967fb11e80112e00725`; the downloaded audit JSON SHA-256 is `4697c1834179af61c70d12325f0527479c5a78d9cb746af7341e36d579667da9`.

Medium target: 10,000,000 cells; allowed range: 8,000,000–12,000,000; cubic projection from the measured coarse core: 9,640,375.

RANS and fine mesh remain prohibited until the generated medium mesh independently passes every production quality and readback gate.
