# Marketing Mix Model on Microsoft Fabric: Runbook

Built 2026-11-08 on a Fabric trial capacity, in its own workspace.

## Phase 1: Infrastructure
- `scripts/setup_fabric.py`: workspace `marketing-mix-analytics` on the trial capacity, Lakehouse
  `lh_marketing` (schemas on), Warehouse `wh_marketing_gold`; IDs written to `config.json`.
- **Done when:** `config.json` has every ID.

## Phase 2: Data
- `scripts/generate_data.py`, then `scripts/upload_landing.py`.
- **Done when:** upload sizes match local files byte for byte.

## Phase 3: Bronze and Silver
- `01_bronze`: 3 tables with `_row_hash`, run log, source line count = rows loaded.
- `02_silver`: 19 rules (14 critical, 5 warning), `silver_quarantine`, `ops_dq_exceptions`, star schema
  (`dim_date`, `dim_region`, `dim_channel`, `dim_product`, `fact_media_daily`, `fact_sales_daily`, `fact_calendar_daily`).
- **Done when:** Bronze = Silver + quarantine for rows, spend (51,401,222) and revenue (327,671,386).

## Phase 4: Model
- `03_mmm_model` includes `scripts/mmm_core.py`; writes `mmm_decomposition`, `mmm_channel_roi`,
  `mmm_response_curves`, `mmm_budget_scenarios`, `mmm_model_fit`, `mmm_model_metrics`.
- **Done when:** holdout MAPE < 10% (0.63%), decomposition closes exactly, ROI compared with ground truth
  and every gap stated (Display +26%, Email -15%, others within 7%).

## Phase 4b: Platforms, campaigns and campaign ML (2026-11-08)
- Data at campaign grain (28 campaigns, 15 platforms), channel totals unchanged (checked against the previous
  ground truth). Silver validates campaign and platform against `campaigns.csv`.
- `03b_campaign_ml` includes `scripts/ml_core.py`: trends with FDR control, changepoints, peer benchmarks,
  KMeans segments, recommendations rescaled to the MMM optimizer's channel ratios.
- **Done when:** 10/10 planted issues found with the right reason, 0 false alarms on 13 healthy campaigns,
  every channel's campaign plan equals the optimizer ratio, Silver campaign spend = Gold campaign spend.

## Phase 5: Gold and controls
- `sql/01_ops_and_gold.sql`: Gold weekly tables (media 21,840 campaign-week-region rows from 152,880 daily; sales 2,340 from 16,380),
  precomputed trend table (4-week averages, same week last year), model outputs, `ops` schema and procs.
- **Done when:** all `ops.usp_reconcile` checks PASS (95/95 with campaign and platform checks), no alert.

## Phase 6: Pipeline
- `scripts/build_pipeline.py --run`: `pl_marketing_mix_weekly`, Bronze -> Silver -> model -> sync -> Gold and
  reconcile -> If ALERT, shared Spark session and retries.
- **Done when:** run Completed with all steps Succeeded (run `71c478fc`).

## Phase 7: Semantic model and report
- `scripts/build_model.py`: Direct Lake `sm_marketing_mix`; DAX check prints revenue, spend, contribution, ROAS,
  YoY, MAPE, uplift. `scripts/build_report.py`: 10 pages, theme `VibrantMarketing`, fixed channel colours.
- **Done when:** DAX totals equal Gold (revenue 327.67M, spend 51.40M, contribution 101.25M), every page renders,
  the budget slicer changes the optimizer (same budget +6.4%, +30% budget +28.2%).

## Notes and lessons
- Measure names must not match column names (case-insensitive); `Trend` as a table name breaks DAX, so the
  table is `Weekly Trend`.
- Budget measures default to the "+0%" scenario when no single scenario is selected.
- Slicers filter out the blank member Direct Lake adds to dimensions.
- Without regional variation and geo-holdout tests, always-on channels (Search, Email) were not identifiable
  (Search ROI came out 5.0 vs 3.2). That is the main modelling lesson of this build.
- Alert email is not wired: the tenant account used for the build has no mailbox.
