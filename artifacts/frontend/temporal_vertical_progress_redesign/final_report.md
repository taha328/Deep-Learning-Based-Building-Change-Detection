# Final Report

## Problem

The temporal project progress panel could show pair/model progress at `100 %` while the backend was still publishing layers, writing artifacts, updating metadata, or cleaning temporary files. The UI compressed that state into two horizontal progress bars and a small `Finalisation` label, which made users think the project was done or stuck.

## UX Design Decision

The temporal workflow now uses a vertical stage timeline. Analysis progress remains visible, but it is scoped inside the `Inférence` stage. Project finalization has its own visible stages: `Publication des couches`, `Génération des exports`, `Écriture des métadonnées`, and `Nettoyage`. Readiness is shown separately at the bottom so `100 %` analysis is never treated as finished results.

## Progress Mapping

The mapping lives in `frontend/src/lib/run-progress.ts` as `buildTemporalProgressTimeline`.

Key behavior:

- `queued` activates `En attente`.
- `preflight` and project validation activate `Préparation du projet`.
- `saving_artifacts` activates `Publication des couches`.
- `persisting` activates `Écriture des métadonnées`.
- `phase=complete` or completed/succeeded status activates `Terminé`.
- `pair_fraction=1.0` with a non-terminal job shows `Analyse terminée — finalisation en cours.` and keeps `Terminé` pending.
- failed/cancelled states mark the active stage as failed and keep future stages pending.

Full mapping details are in `progress_mapping.md`.

## Files Changed

- `frontend/src/lib/run-progress.ts`
- `frontend/src/lib/run-progress.test.ts`
- `frontend/src/features/results/TemporalVerticalProgressTimeline.tsx`
- `frontend/src/features/results/RunProgressPanel.tsx`
- `frontend/src/features/temporal/TemporalMosaicPanel.tsx`
- `artifacts/frontend/temporal_vertical_progress_redesign/*`

## Tests

- `npm test --prefix frontend`: `117` passed
- `npm run build --prefix frontend`: passed
- `backend/.venv/bin/python -m pytest backend/tests -q`: `563` passed, `5` skipped

See `test_results.md` for logs and details.

## Manual Validation

Manual validation used the captured Casa 2 `saving_artifacts` state:

- `status=running`
- `stage=saving_artifacts`
- `pair_fraction=1.0`
- `result_run_id=null`

The timeline shows `Publication des couches` as active, `Terminé` as pending, and `Résultats pas encore prêts.` at the bottom. Completed and failed variants were also validated. Evidence is in `manual_validation.md`, `manual_validation_states.json`, and the three small screenshots.

## Remaining Risks

- The backend currently emits only coarse finalization stages (`saving_artifacts`, `persisting`). The UI exposes export/metadata/cleanup stages honestly as pending unless the backend stage text reaches them.
- Completed progress may be visible only briefly before the existing workflow replaces the progress panel with results. This preserves the current completed-run behavior while still rendering `Résultats prêts.` for terminal job progress states.
