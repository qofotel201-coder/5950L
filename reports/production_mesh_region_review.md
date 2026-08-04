# Production mesh region review

Status: **PASS**. The coarse partition is frozen and the medium mesh is authorized but not built.

| Region | Coarse size (m) | Medium size (m) | Transition (m) |
|---|---:|---:|---:|
| farfield | 0.054221688713527318 | 0.043052020838540692 | 0.5 |
| transition | 0.036147792475684878 | 0.028701347225693796 | 0.5 |
| near_body_core | 0.024098528317123252 | 0.019134231483795863 | 0.29999999999999999 |
| internal_passage | 0.016567738218022234 | 0.013154784145109654 | 0.20000000000000001 |

The size order is strictly farfield > transition > near-body > internal passage. Bounds are nested, transitions are linear and positive, and the volume callback composes overlaps by minimum size. The frozen wall retains 60 layers, 0.4 m tangential triangulation, first-layer height, growth ratio, and Pilot repair parameters.

Medium target: 10,000,000 cells; allowed range: 8,000,000–12,000,000; cubic projection from the measured coarse core: 9,640,375.

RANS and fine mesh remain prohibited until the generated medium mesh independently passes every production quality and readback gate.
