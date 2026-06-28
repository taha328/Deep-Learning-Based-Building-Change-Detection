# Before State

- Project: `temporal-casa-mqwqi0mf-7e7dwl`
- Failed job: `job-ed191350424848178a8380405ad61b33`
- Failure mode: project stopped after the large `WB_2025_R03` pair with a large-result/summary-backed finalization error instead of continuing to `WB_2026_R05`.
- Initial persisted milestones:
  - `WB_2024_R02`: complete baseline, empty additions.
  - `WB_2025_R03`: complete, `additions.geojson` file-backed, 157,087,847 bytes, 17,040 features, tilejson available.
  - `WB_2026_R05`: validated/pending.

The compact project payload was pending overall because the completed large pair was treated as a stop condition for remaining temporal pairs.
