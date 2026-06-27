# Full Temporal Project Persistent Runner Validation Plan

## Scope

Run one real temporal project with at least three milestones through `run_temporal_project_api` using `APP_BANDON_INFERENCE_MODE=persistent_runner`.

## Safety

- Runtime outputs are isolated under the validation artifact runtime directory.
- Existing runtime cache and previous benchmark artifacts are not deleted or overwritten.
- The rollback mode remains `APP_BANDON_INFERENCE_MODE=cli_per_tile`.

## Project

- Source project JSON: `backend/runtime_cache/temporal_projects/temporal-test-mqox4hjy-yje2r5/project.json`
- AOI source: `bbox:[-7.6176452637, 33.487581245, -7.5956726074, 33.505904621]`
- Validation project ID: `temporal-persistent-runner-tiled-validation-20260627T084048Z`
- Milestones: `WB_2018_R03, WB_2021_R04, WB_2026_R05`
- Runtime cache: `/Users/tahaelouali/Developer/Building_change_app/artifacts/benchmarks/bandon_persistent_runner_full_temporal_project_validation/runtime/tiled_20260627T084048Z`

## Expected Pairs

| Pair | From | To | Estimated Wayback Tiles |
| --- | --- | --- | ---: |
| 1 | WB_2018_R03 | WB_2021_R04 | 544 |
| 2 | WB_2021_R04 | WB_2026_R05 | 544 |

## Required Report Artifacts

- `validation_plan.md`
- `temporal_project_run_metadata.json`
- `pair_summaries.json`
- `pair_timing_summaries_index.json`
- `memory_profile_across_pairs.json`
- `progress_samples.json`
- `pair_output_validation_report.json`
- `final_export_validation_report.json`
- `full_temporal_project_validation_report.md`
- `acceptance_decision.md`
