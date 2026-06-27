# Manual Validation

Date: 2026-06-27

## Method

The Casa 2 historical payload captured the exact confusing state:

```json
{
  "status": "running",
  "stage": "saving_artifacts",
  "progress": 92,
  "message": "Completed",
  "progress_details": {
    "pair_fraction": 1.0,
    "current_pair_index": 1,
    "total_pair_count": 2,
    "pair_stage": "Completed",
    "from_release_identifier": "WB_2024_R02",
    "to_release_identifier": "WB_2025_R03"
  },
  "result_run_id": null
}
```

I rendered the actual `TemporalVerticalProgressTimeline` component in a temporary Vite harness with this captured Casa-style state, plus completed and failed variants. The harness file was removed after screenshots were captured.

## Evidence Files

- `manual_validation_states.json`
- `manual_finalizing.png`
- `manual_completed.png`
- `manual_failed.png`

## Cases

### Casa 2 Saving Artifacts / Finalization

Result: passed.

- Vertical stages are visible.
- Pair analysis shows `100 %`.
- Global analysis shows `50 %` for pair `1/2`.
- `Publication des couches` is active.
- `Terminé` is pending.
- The panel says `Analyse terminée — finalisation en cours.`
- The readiness block says `Résultats pas encore prêts.`
- The readiness detail says layers will be available after final publication.

### Completed Project

Result: passed.

- Every stage is marked complete.
- Readiness says `Résultats prêts.`
- The final detail says published layers can be consulted on the map.

### Failed During Finalization

Result: passed.

- `Publication des couches` is marked failed.
- Later stages remain pending.
- Readiness says `Résultats non disponibles.`
- The backend error detail remains visible.

### Queued Project

Result: passed by automated mapping test.

- `En attente` is active.
- No fake pair/global percent is invented.

## UX Check

The new panel no longer presents `100 %` as the only meaningful state. It shows analysis progress as supporting context inside the `Inférence` stage and keeps final result readiness separate at the bottom.
