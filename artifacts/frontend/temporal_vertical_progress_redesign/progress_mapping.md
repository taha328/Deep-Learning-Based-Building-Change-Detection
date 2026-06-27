# Temporal Progress Mapping

## Design Rule

`pair_fraction=1.0` means the current pair analysis is complete. It does not mean the temporal project is ready. The UI only shows `Résultats prêts` when the job is terminally completed.

## Inputs

The mapping uses `RunProgressState`:

- `phase`
- `percent`
- `stageLabel`
- `detail`
- `rawEvent`
- `temporalPairDetails`

## Stage Mapping

- `status=queued` -> `En attente`
- `stage=starting`, `preflight`, `metadata`, or project validation text -> `Préparation du projet`
- explicit tile availability text -> `Vérification des tuiles`
- `fetching_imagery`, `download`, `imagery`, `mosaic`, `Wayback`, `reference` -> `Téléchargement des images`
- `alignment` -> `Alignement`
- `inference`, `BANDON`, detection text -> `Inférence`
- `postprocess`, filtering, consolidation text -> `Post-traitement`
- `vectorizing`, `vectorization` -> `Vectorisation`
- `saving_artifacts`, `building_buffers`, publication/layer/artifact text -> `Publication des couches`
- export/bundle/QGIS/GeoPackage/report text -> `Génération des exports`
- `persisting`, compact metadata, manifest, summary, database text -> `Écriture des métadonnées`
- cleanup text -> `Nettoyage`
- `phase=complete` or completed/succeeded raw status -> `Terminé`

## Critical Cases

### Pair Complete, Finalization Running

Input:

- `phase=running`
- `stageLabel=saving_artifacts`
- `pair_fraction=1.0`

UI:

- analysis rows before publication are complete
- `Publication des couches` is active
- `Terminé` remains pending
- readiness says `Résultats pas encore prêts`
- summary says `Analyse terminée — finalisation en cours.`

### Job Completed

Input:

- `phase=complete` or completed/succeeded raw status

UI:

- every stage is complete
- active stage is `Terminé`
- readiness says `Résultats prêts.`

### Failed During Finalization

Input:

- `phase=error` or failed raw status
- `stageLabel=saving_artifacts` or publication text

UI:

- publication is marked failed
- later stages remain pending
- readiness says `Résultats non disponibles`
- backend error detail remains visible

### Queued

Input:

- `phase=queued`

UI:

- `En attente` is active
- no pair/global percent is invented
- later stages remain pending

## No Backend Change

The current API distinguishes enough state for an honest frontend mapping. This change does not alter thresholds, inference, persistence, cleanup, artifacts, or worker execution.
