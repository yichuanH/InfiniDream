# scenes/ — baseline Infinigen scenes

The pipeline edits an existing Infinigen scene. Each scene lives at
`scenes/<biome>/seed<N>/` and needs at least `coarse/scene.blend`
(`fine/scene.blend` is optional: it only gives the Director a nicer preview).

Build one with:

    bash scripts/build_scene.sh <biome> <seed>        # e.g. plain 0

Biomes available in Infinigen nature: forest desert coast mountain plain canyon
cliff river arctic snowy_mountain cave coral_reef under_water kelp_forest.
