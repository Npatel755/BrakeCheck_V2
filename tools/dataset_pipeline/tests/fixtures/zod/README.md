These two native annotation fixtures correspond to the user's reported
`zod-000001` and `zod-000002` converter failures. They come from the public
[8bits-ai/ZOD-Mini-2D-Road-Scenes mirror](https://huggingface.co/datasets/8bits-ai/ZOD-Mini-2D-Road-Scenes)
at revision `244c5a10859c6951830112fdb97072b83dddf92e` and retain the source
CC-BY-SA-4.0 licence. They are annotations only; photos remain in the isolated
downloaded collection. The tests check long-boundary retention and rejection
of unsupported sparse lane-slot assignments, not a held-out accuracy score.

The `overlap-*.json` fixtures retain source-derived lane geometry and observed
paint sections for the reported 004719, 005033, 005909 and 006567 examples under
the same CC-BY-SA-4.0 licence. They exercise paired-stripe consolidation and
preservation of distinct or ambiguous neighboring boundaries.
