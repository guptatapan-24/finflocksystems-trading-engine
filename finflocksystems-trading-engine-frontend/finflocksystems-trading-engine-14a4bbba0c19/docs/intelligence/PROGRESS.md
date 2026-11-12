# Intelligence layer: frontend progress

Source of truth for the plan: backend `docs/intelligence/PROGRESS.md` and `CLAUDE.md`.

| Phase | Frontend status |
|---|---|
| 0 Discovery | **Done**: FRONTEND_NOTES.md (docs only, zero source changes) |
| 1 Skeleton | **Done**: `src/features/intelligence/{types,queries}.ts` + 1 test; no screen, no existing file touched |
| 2 Episodes | Not started |
| 3 Reviews and suggestions | **Done**: ReviewPanel in the episode drawer |
| 4 Live monitor and report | **Done**: Live trades page, report page, intent note |
| 5 Learning feed | **Done**: Learning tab (feed, lessons, performance, calibration) |
| 6 Advisory | **Done**: advice banner (wizard, edit panel), Weekly and What-if views |

Engine/app files touched in Phase 0: none. New files: `docs/intelligence/FRONTEND_NOTES.md`, `docs/intelligence/PROGRESS.md`.
Baseline: tsc clean; vitest 80 passed / 12 failed before any change.
