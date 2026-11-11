-- Gold layer and operational controls for the marketing mix model.
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'gold') EXEC('CREATE SCHEMA gold');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'ops') EXEC('CREATE SCHEMA ops');
GO

IF OBJECT_ID('ops.pipeline_run_log') IS NULL
CREATE TABLE ops.pipeline_run_log (
    run_id varchar(100), layer varchar(20), step varchar(200), status varchar(20),
    rows_in bigint, rows_out bigint, started_at datetime2(6), ended_at datetime2(6), message varchar(4000));
IF OBJECT_ID('ops.recon_results') IS NULL
CREATE TABLE ops.recon_results (
    run_id varchar(100), check_name varchar(200), layer_from varchar(20), layer_to varchar(20),
    dimension varchar(50), dim_value varchar(100), metric varchar(50),
    expected decimal(38,2), actual decimal(38,2), diff decimal(38,2), status varchar(10), checked_at datetime2(6));
IF OBJECT_ID('ops.dq_exceptions') IS NULL
CREATE TABLE ops.dq_exceptions (
    run_id varchar(100), layer varchar(20), source_table varchar(100), source_row_hash bigint,
    business_key varchar(100), rule_name varchar(100), severity varchar(20), column_name varchar(100),
    raw_value varchar(4000), action varchar(30), logged_at datetime2(6));
IF OBJECT_ID('ops.alert_log') IS NULL
CREATE TABLE ops.alert_log (
    alert_id varchar(100), run_id varchar(100), raised_at datetime2(6), severity varchar(20),
    alert_type varchar(50), summary varchar(4000), rca_hint varchar(4000), recipient varchar(200),
    delivery_status varchar(30), delivered_at datetime2(6));
GO

CREATE OR ALTER PROCEDURE ops.usp_log_step
    @run_id varchar(100), @layer varchar(20), @step varchar(200), @status varchar(20),
    @rows_in bigint, @rows_out bigint, @started_at datetime2(6), @message varchar(4000)
AS
BEGIN
    INSERT INTO ops.pipeline_run_log
    VALUES (@run_id, @layer, @step, @status, @rows_in, @rows_out, @started_at, SYSUTCDATETIME(), @message);
END;
GO

CREATE OR ALTER PROCEDURE ops.usp_load_gold @run_id varchar(100)
AS
BEGIN
    DECLARE @started datetime2(6) = SYSUTCDATETIME(), @rows bigint, @msg varchar(4000);
    BEGIN TRY
        DROP TABLE IF EXISTS gold.dim_week;
        DROP TABLE IF EXISTS gold.dim_channel;
        DROP TABLE IF EXISTS gold.dim_region;
        DROP TABLE IF EXISTS gold.dim_component;
        DROP TABLE IF EXISTS gold.fact_weekly_media;
        DROP TABLE IF EXISTS gold.fact_weekly_sales;
        DROP TABLE IF EXISTS gold.fact_weekly_trend;
        DROP TABLE IF EXISTS gold.fact_decomposition;
        DROP TABLE IF EXISTS gold.fact_model_fit;
        DROP TABLE IF EXISTS gold.agg_channel_roi;
        DROP TABLE IF EXISTS gold.agg_response_curves;
        DROP TABLE IF EXISTS gold.agg_budget_scenarios;
        DROP TABLE IF EXISTS gold.agg_model_metrics;
        DROP TABLE IF EXISTS gold.fact_campaign_weekly;

        CREATE TABLE gold.dim_week AS
        WITH w AS (SELECT DISTINCT week_start FROM lh_marketing.dbo.fact_sales_daily),
             holdout AS (SELECT DISTINCT CAST(week AS date) AS week_start, CAST(is_holdout AS int) AS is_holdout
                         FROM lh_marketing.dbo.mmm_model_fit)
        SELECT w.week_start, YEAR(w.week_start) AS year, CONCAT('Q', DATEPART(quarter, w.week_start)) AS quarter,
               MONTH(w.week_start) AS month, CAST(LEFT(DATENAME(month, w.week_start), 3) AS varchar(3)) AS month_name,
               CONCAT(YEAR(w.week_start), '-W', RIGHT(CONCAT('0', DATEPART(iso_week, w.week_start)), 2)) AS week_label,
               DATEPART(iso_week, w.week_start) AS iso_week, ISNULL(h.is_holdout, 0) AS is_holdout
        FROM w LEFT JOIN holdout h ON w.week_start = h.week_start;

        CREATE TABLE gold.dim_channel AS
        SELECT channel, channel_group, channel_color, CAST(channel_sort AS int) AS channel_sort
        FROM lh_marketing.dbo.dim_channel;
        CREATE TABLE gold.dim_region AS SELECT region FROM lh_marketing.dbo.dim_region;
        DROP TABLE IF EXISTS gold.dim_campaign;
        CREATE TABLE gold.dim_campaign AS
        SELECT c.campaign, c.platform, c.channel, c.objective, ISNULL(s.segment, 'Not click-tracked') AS segment
        FROM lh_marketing.dbo.dim_campaign c LEFT JOIN lh_marketing.dbo.ml_segments s ON c.campaign = s.campaign;
        DROP TABLE IF EXISTS gold.dim_platform;
        CREATE TABLE gold.dim_platform AS
        SELECT p.platform, p.channel, p.platform_type, CAST(c.channel_sort AS int) AS channel_sort
        FROM lh_marketing.dbo.dim_platform p JOIN lh_marketing.dbo.dim_channel c ON p.channel = c.channel;
        CREATE TABLE gold.dim_component AS
        SELECT 'Baseline' AS component, 'Baseline' AS component_group, '#94A3B8' AS component_color, 0 AS component_sort
        UNION ALL
        SELECT channel, channel_group, channel_color, CAST(channel_sort AS int) FROM lh_marketing.dbo.dim_channel;

        -- Campaign grain. The model measures channels, so each channel's weekly contribution is allocated to
        -- its campaigns by their share of that channel's spend in the same region-week (never used to rank campaigns).
        CREATE TABLE gold.fact_weekly_media AS
        WITH m AS (
            SELECT week_start, region, channel, platform, campaign, SUM(spend) AS spend, SUM(impressions) AS impressions,
                   SUM(clicks) AS clicks, SUM(conversions) AS conversions, SUM(video_views) AS video_views,
                   SUM(attributed_revenue) AS attributed_revenue
            FROM lh_marketing.dbo.fact_media_daily GROUP BY week_start, region, channel, platform, campaign
        ), ch AS (
            SELECT week_start, region, channel, SUM(spend) AS channel_spend, COUNT(*) AS platforms
            FROM m GROUP BY week_start, region, channel
        ), d AS (
            SELECT CAST(week AS date) AS week_start, region, component AS channel, contribution
            FROM lh_marketing.dbo.mmm_decomposition WHERE component <> 'Baseline'
        )
        SELECT m.week_start, m.region, m.channel, m.platform, m.campaign, CAST(m.spend AS decimal(18,2)) AS spend,
               CAST(m.impressions AS bigint) AS impressions, CAST(m.clicks AS bigint) AS clicks,
               CAST(m.conversions AS bigint) AS conversions, CAST(m.video_views AS bigint) AS video_views,
               CAST(m.attributed_revenue AS decimal(18,2)) AS attributed_revenue,
               CAST(ISNULL(d.contribution, 0) * CASE WHEN ch.channel_spend > 0 THEN m.spend / ch.channel_spend
                                                     ELSE 1.0 / ch.platforms END AS decimal(18,2)) AS contribution
        FROM m
        JOIN ch ON m.week_start = ch.week_start AND m.region = ch.region AND m.channel = ch.channel
        LEFT JOIN d ON m.week_start = d.week_start AND m.region = d.region AND m.channel = d.channel;

        CREATE TABLE gold.fact_weekly_sales AS
        SELECT week_start, region, product, CAST(SUM(revenue) AS decimal(18,2)) AS revenue,
               CAST(SUM(units) AS bigint) AS units
        FROM lh_marketing.dbo.fact_sales_daily GROUP BY week_start, region, product;

        -- Trend helpers precomputed per week x region: 4-week moving sums and same ISO week last year
        CREATE TABLE gold.fact_weekly_trend AS
        WITH base AS (
            SELECT s.week_start, s.region, SUM(s.revenue) AS revenue,
                   (SELECT SUM(m.spend) FROM gold.fact_weekly_media m
                    WHERE m.week_start = s.week_start AND m.region = s.region) AS spend
            FROM gold.fact_weekly_sales s GROUP BY s.week_start, s.region
        ), w AS (
            SELECT b.*, DATEPART(iso_week, week_start) AS iso_week, YEAR(week_start) AS yr,
                   SUM(revenue) OVER (PARTITION BY region ORDER BY week_start ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS revenue_4wk,
                   SUM(spend) OVER (PARTITION BY region ORDER BY week_start ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS spend_4wk,
                   COUNT(*) OVER (PARTITION BY region ORDER BY week_start ROWS BETWEEN 3 PRECEDING AND CURRENT ROW) AS weeks_in_window
            FROM base b
        )
        SELECT cur.week_start, cur.region, CAST(cur.revenue AS decimal(18,2)) AS revenue,
               CAST(cur.spend AS decimal(18,2)) AS spend,
               CAST(cur.revenue_4wk / cur.weeks_in_window AS decimal(18,2)) AS revenue_ma4,
               CAST(cur.spend_4wk / cur.weeks_in_window AS decimal(18,2)) AS spend_ma4,
               CAST(ly.revenue AS decimal(18,2)) AS revenue_last_year,
               CAST(ly.spend AS decimal(18,2)) AS spend_last_year
        FROM w cur LEFT JOIN w ly
          ON ly.region = cur.region AND ly.iso_week = cur.iso_week AND ly.yr = cur.yr - 1;

        CREATE TABLE gold.fact_decomposition AS
        SELECT CAST(week AS date) AS week_start, region, component, CAST(contribution AS decimal(18,2)) AS contribution
        FROM lh_marketing.dbo.mmm_decomposition;

        CREATE TABLE gold.fact_model_fit AS
        SELECT CAST(week AS date) AS week_start, CAST(actual AS decimal(18,2)) AS actual,
               CAST(predicted AS decimal(18,2)) AS predicted, CAST(is_holdout AS int) AS is_holdout,
               CASE WHEN is_holdout = 1 THEN 'Holdout (last 12 weeks)' ELSE 'Training' END AS sample,
               CAST(ABS(actual - predicted) / NULLIF(actual, 0) AS decimal(9,6)) AS abs_pct_error
        FROM lh_marketing.dbo.mmm_model_fit;

        CREATE TABLE gold.agg_channel_roi AS
        SELECT r.channel, c.channel_group, CAST(r.spend AS decimal(18,2)) AS spend,
               CAST(r.contribution AS decimal(18,2)) AS contribution, CAST(r.roi AS decimal(9,4)) AS roi,
               CAST(r.marginal_roi AS decimal(9,4)) AS marginal_roi, CAST(r.true_roi AS decimal(9,4)) AS true_roi,
               CAST(r.roi_error_pct AS decimal(9,2)) AS roi_error_pct, CAST(r.spend_share AS decimal(9,6)) AS spend_share,
               CAST(r.contribution_share AS decimal(9,6)) AS contribution_share, CAST(r.decay AS decimal(5,2)) AS decay,
               CAST(r.hill_shape AS decimal(5,2)) AS hill_shape, CAST(r.half_saturation AS decimal(5,2)) AS half_saturation
        FROM lh_marketing.dbo.mmm_channel_roi r JOIN lh_marketing.dbo.dim_channel c ON r.channel = c.channel;

        CREATE TABLE gold.agg_response_curves AS
        SELECT channel, CAST(spend_multiplier AS decimal(5,2)) AS spend_multiplier, CAST(spend AS decimal(18,2)) AS spend,
               CAST(contribution AS decimal(18,2)) AS contribution, CAST(is_current AS int) AS is_current,
               CAST(contribution / NULLIF(spend, 0) AS decimal(9,4)) AS roi_at_point
        FROM lh_marketing.dbo.mmm_response_curves;

        CREATE TABLE gold.agg_budget_scenarios AS
        SELECT scenario, CAST(budget_change AS decimal(5,2)) AS budget_change,
               CAST(ROUND(budget_change * 10, 0) AS int) AS scenario_sort, channel,
               CAST(current_spend AS decimal(18,2)) AS current_spend, CAST(optimal_spend AS decimal(18,2)) AS optimal_spend,
               CAST(current_contribution AS decimal(18,2)) AS current_contribution,
               CAST(optimal_contribution AS decimal(18,2)) AS optimal_contribution
        FROM lh_marketing.dbo.mmm_budget_scenarios;

        DROP TABLE IF EXISTS gold.fact_campaign_weekly;
        DROP TABLE IF EXISTS gold.agg_campaign_recommendations;
        DROP TABLE IF EXISTS gold.agg_campaign_trends;
        DROP TABLE IF EXISTS gold.agg_campaign_changepoints;
        DROP TABLE IF EXISTS gold.agg_ml_evaluation;

        CREATE TABLE gold.fact_campaign_weekly AS
        SELECT CAST(week AS date) AS week_start, campaign, CAST(spend AS decimal(18,2)) AS spend,
               CAST(impressions AS bigint) AS impressions, CAST(clicks AS bigint) AS clicks,
               CAST(conversions AS bigint) AS conversions, CAST(attributed_revenue AS decimal(18,2)) AS attributed_revenue
        FROM lh_marketing.dbo.ml_campaign_weekly;

        CREATE TABLE gold.agg_campaign_recommendations AS
        SELECT campaign, platform, channel, objective, reason, action, confidence, CAST(evidence AS varchar(2000)) AS evidence,
               CAST(recent_spend AS decimal(18,2)) AS recent_spend, CAST(recommended_spend AS decimal(18,2)) AS recommended_spend,
               CAST(budget_change AS decimal(18,2)) AS budget_change, CAST(budget_change_pct AS decimal(9,4)) AS budget_change_pct,
               CAST(within_channel_change_pct AS decimal(9,4)) AS within_channel_change_pct,
               CAST(channel_ratio AS decimal(9,4)) AS channel_ratio,
               CAST(cpa AS decimal(18,2)) AS cpa, CAST(recent_cpa AS decimal(18,2)) AS recent_cpa,
               CAST(platform_roas AS decimal(9,4)) AS platform_roas, CAST(ctr AS decimal(9,6)) AS ctr,
               CAST(cvr AS decimal(9,6)) AS cvr, CAST(cpm AS decimal(18,2)) AS cpm, CAST(cpc AS decimal(18,2)) AS cpc,
               CASE action WHEN 'Scale' THEN 1 WHEN 'Reduce' THEN 2 WHEN 'Refresh creative' THEN 3 WHEN 'Investigate' THEN 4
                           ELSE 5 END AS action_sort
        FROM lh_marketing.dbo.ml_recommendations;

        CREATE TABLE gold.agg_campaign_trends AS
        SELECT campaign, UPPER(kpi) AS kpi, CAST(relative_change AS decimal(9,4)) AS relative_change,
               CAST(start_value AS decimal(18,6)) AS start_value, CAST(end_value AS decimal(18,6)) AS end_value,
               CAST(q_value AS float) AS q_value, CAST(CASE WHEN significant = 'True' OR significant = '1' THEN 1 ELSE 0 END AS int) AS significant
        FROM lh_marketing.dbo.ml_trends;

        CREATE TABLE gold.agg_campaign_changepoints AS
        SELECT campaign, UPPER(kpi) AS kpi, CAST(week AS date) AS change_week, CAST(before AS decimal(18,6)) AS before_value,
               CAST(after AS decimal(18,6)) AS after_value, CAST(shift AS decimal(9,4)) AS shift
        FROM lh_marketing.dbo.ml_changepoints;

        CREATE TABLE gold.agg_ml_evaluation AS
        SELECT campaign, planted_issue, expected_reason, detected_reason,
               CAST(CASE WHEN correct = 'True' OR correct = '1' THEN 1 ELSE 0 END AS int) AS correct
        FROM lh_marketing.dbo.ml_evaluation;

        CREATE TABLE gold.agg_model_metrics AS
        SELECT CAST(train_mape AS decimal(9,6)) AS train_mape, CAST(holdout_mape AS decimal(9,6)) AS holdout_mape,
               CAST(train_r2 AS decimal(9,6)) AS train_r2, CAST(media_contribution AS decimal(18,2)) AS media_contribution,
               CAST(true_media_contribution AS decimal(18,2)) AS true_media_contribution,
               CAST(max_abs_roi_error_pct AS decimal(9,2)) AS max_abs_roi_error_pct, weeks, holdout_weeks, run_id
        FROM lh_marketing.dbo.mmm_model_metrics;

        SELECT @rows = COUNT_BIG(*) FROM gold.fact_weekly_media;
        SET @msg = CONCAT('weekly media rows ', @rows);
        EXEC ops.usp_log_step @run_id, 'gold', 'load gold', 'SUCCEEDED', NULL, @rows, @started, @msg;
    END TRY
    BEGIN CATCH
        SET @msg = ERROR_MESSAGE();
        EXEC ops.usp_log_step @run_id, 'gold', 'load gold', 'ERROR', NULL, NULL, @started, @msg;
        THROW;
    END CATCH
END;
GO

-- Reconciles data and model outputs for one run, raises alerts, returns one status row. Never throws on a mismatch.
CREATE OR ALTER PROCEDURE ops.usp_reconcile @run_id varchar(100), @recipient varchar(200), @mape_threshold decimal(9,4) = 0.10
AS
BEGIN
    DECLARE @started datetime2(6) = SYSUTCDATETIME(), @now datetime2(6) = SYSUTCDATETIME();

    INSERT INTO ops.pipeline_run_log
    SELECT run_id, layer, step, status, rows_in, rows_out, started_at, ended_at, message
    FROM lh_marketing.dbo.ops_run_log WHERE run_id = @run_id;
    INSERT INTO ops.dq_exceptions
    SELECT run_id, layer, source_table, source_row_hash, business_key, rule_name, severity, column_name,
           raw_value, action, logged_at
    FROM lh_marketing.dbo.ops_dq_exceptions WHERE run_id = @run_id;

    -- Source -> Bronze
    INSERT INTO ops.recon_results
    SELECT @run_id, 'rows ' + step, 'source', 'bronze', 'table', REPLACE(step, 'load ', ''), 'rows',
           rows_in, rows_out, rows_out - rows_in, CASE WHEN rows_in = rows_out THEN 'PASS' ELSE 'FAIL' END, @now
    FROM lh_marketing.dbo.ops_run_log WHERE run_id = @run_id AND layer = 'bronze';

    -- Bronze -> Silver: rows and money, quarantine included
    WITH q AS (
        SELECT source_table, COUNT_BIG(*) AS n,
               SUM(TRY_CAST(JSON_VALUE(raw_record, '$.spend') AS decimal(38,2))) AS spend,
               SUM(TRY_CAST(JSON_VALUE(raw_record, '$.revenue') AS decimal(38,2))) AS revenue
        FROM lh_marketing.dbo.silver_quarantine GROUP BY source_table
    ), checks AS (
        SELECT 'media rows' AS check_name, (SELECT COUNT_BIG(*) FROM lh_marketing.dbo.bronze_media_spend) AS e,
               (SELECT COUNT_BIG(*) FROM lh_marketing.dbo.fact_media_daily)
               + ISNULL((SELECT n FROM q WHERE source_table = 'bronze_media_spend'), 0) AS a
        UNION ALL
        SELECT 'sales rows', (SELECT COUNT_BIG(*) FROM lh_marketing.dbo.bronze_sales),
               (SELECT COUNT_BIG(*) FROM lh_marketing.dbo.fact_sales_daily)
               + ISNULL((SELECT n FROM q WHERE source_table = 'bronze_sales'), 0)
        UNION ALL
        SELECT 'media spend', (SELECT SUM(TRY_CAST(spend AS decimal(38,2))) FROM lh_marketing.dbo.bronze_media_spend),
               (SELECT SUM(CAST(spend AS decimal(38,2))) FROM lh_marketing.dbo.fact_media_daily)
               + ISNULL((SELECT spend FROM q WHERE source_table = 'bronze_media_spend'), 0)
        UNION ALL
        SELECT 'sales revenue', (SELECT SUM(TRY_CAST(revenue AS decimal(38,2))) FROM lh_marketing.dbo.bronze_sales),
               (SELECT SUM(CAST(revenue AS decimal(38,2))) FROM lh_marketing.dbo.fact_sales_daily)
               + ISNULL((SELECT revenue FROM q WHERE source_table = 'bronze_sales'), 0)
    )
    INSERT INTO ops.recon_results
    SELECT @run_id, 'bronze = silver + quarantine', 'bronze', 'silver', 'all', check_name, check_name, e, a, a - e,
           CASE WHEN ABS(a - e) < 1 THEN 'PASS' ELSE 'FAIL' END, @now
    FROM checks;

    -- Silver -> Gold: spend by channel and region, revenue by product and region
    WITH s AS (
        SELECT 'spend by channel' AS check_name, channel AS k, SUM(CAST(spend AS decimal(38,2))) AS v
        FROM lh_marketing.dbo.fact_media_daily GROUP BY channel
        UNION ALL SELECT 'spend by region', region, SUM(CAST(spend AS decimal(38,2))) FROM lh_marketing.dbo.fact_media_daily GROUP BY region
        UNION ALL SELECT 'spend by platform', platform, SUM(CAST(spend AS decimal(38,2))) FROM lh_marketing.dbo.fact_media_daily GROUP BY platform
        UNION ALL SELECT 'spend by campaign', campaign, SUM(CAST(spend AS decimal(38,2))) FROM lh_marketing.dbo.fact_media_daily GROUP BY campaign
        UNION ALL SELECT 'conversions by platform', platform, SUM(CAST(conversions AS decimal(38,2))) FROM lh_marketing.dbo.fact_media_daily GROUP BY platform
        UNION ALL SELECT 'revenue by product', product, SUM(CAST(revenue AS decimal(38,2))) FROM lh_marketing.dbo.fact_sales_daily GROUP BY product
        UNION ALL SELECT 'revenue by region', region, SUM(CAST(revenue AS decimal(38,2))) FROM lh_marketing.dbo.fact_sales_daily GROUP BY region
    ), g AS (
        SELECT 'spend by channel' AS check_name, channel AS k, SUM(spend) AS v FROM gold.fact_weekly_media GROUP BY channel
        UNION ALL SELECT 'spend by region', region, SUM(spend) FROM gold.fact_weekly_media GROUP BY region
        UNION ALL SELECT 'spend by platform', platform, SUM(spend) FROM gold.fact_weekly_media GROUP BY platform
        UNION ALL SELECT 'spend by campaign', campaign, SUM(spend) FROM gold.fact_weekly_media GROUP BY campaign
        UNION ALL SELECT 'conversions by platform', platform, CAST(SUM(conversions) AS decimal(38,2)) FROM gold.fact_weekly_media GROUP BY platform
        UNION ALL SELECT 'revenue by product', product, SUM(revenue) FROM gold.fact_weekly_sales GROUP BY product
        UNION ALL SELECT 'revenue by region', region, SUM(revenue) FROM gold.fact_weekly_sales GROUP BY region
    )
    INSERT INTO ops.recon_results
    SELECT @run_id, 'silver vs gold ' + ISNULL(s.check_name, g.check_name), 'silver', 'gold',
           ISNULL(s.check_name, g.check_name), ISNULL(s.k, g.k), 'amount', ISNULL(s.v, 0), ISNULL(g.v, 0),
           ISNULL(g.v, 0) - ISNULL(s.v, 0), CASE WHEN ABS(ISNULL(g.v, 0) - ISNULL(s.v, 0)) < 1 THEN 'PASS' ELSE 'FAIL' END, @now
    FROM s FULL JOIN g ON s.check_name = g.check_name AND s.k = g.k;

    -- Model: decomposition closes, model actuals equal Gold revenue, contributions equal ROI table
    WITH wk AS (
        SELECT week_start, SUM(contribution) AS parts FROM gold.fact_decomposition GROUP BY week_start
    ), fit AS (SELECT week_start, actual, predicted FROM gold.fact_model_fit),
    rev AS (SELECT week_start, SUM(revenue) AS revenue FROM gold.fact_weekly_sales GROUP BY week_start),
    checks AS (
        SELECT 'decomposition = predicted' AS check_name, SUM(f.predicted) AS e, SUM(w.parts) AS a,
               MAX(ABS(w.parts - f.predicted)) AS worst
        FROM fit f JOIN wk w ON f.week_start = w.week_start
        UNION ALL
        SELECT 'model actual = gold revenue', SUM(r.revenue), SUM(f.actual), MAX(ABS(r.revenue - f.actual))
        FROM fit f JOIN rev r ON f.week_start = r.week_start
        UNION ALL
        SELECT 'channel contribution = ROI table', (SELECT SUM(contribution) FROM gold.agg_channel_roi),
               (SELECT SUM(contribution) FROM gold.fact_weekly_media), NULL
    )
    INSERT INTO ops.recon_results
    SELECT @run_id, check_name, 'model', 'gold', 'all', 'all', 'amount', e, a, a - e,
           CASE WHEN ABS(a - e) < 5 AND ISNULL(worst, 0) < 5 THEN 'PASS' ELSE 'FAIL' END, @now
    FROM checks;

    -- Campaign recommendations land each channel exactly on the MMM optimizer's same-budget ratio
    WITH r AS (SELECT channel, SUM(recommended_spend) / NULLIF(SUM(recent_spend), 0) AS ratio, MAX(channel_ratio) AS target
               FROM gold.agg_campaign_recommendations GROUP BY channel)
    INSERT INTO ops.recon_results
    SELECT @run_id, 'campaign plan = optimizer ratio', 'ml', 'model', 'channel', channel, 'ratio', target, ratio,
           ratio - target, CASE WHEN ABS(ratio - target) < 0.001 THEN 'PASS' ELSE 'FAIL' END, @now
    FROM r;

    -- Model accuracy gate
    INSERT INTO ops.recon_results
    SELECT @run_id, 'holdout MAPE <= threshold', 'model', 'model', 'metric', 'holdout_mape', 'ratio',
           @mape_threshold, holdout_mape, holdout_mape - @mape_threshold,
           CASE WHEN holdout_mape <= @mape_threshold THEN 'PASS' ELSE 'FAIL' END, @now
    FROM gold.agg_model_metrics;

    DECLARE @critical bigint, @rules varchar(4000), @fails bigint, @fail_list varchar(4000), @mape decimal(9,6);
    SELECT @critical = COUNT(*) FROM ops.dq_exceptions WHERE run_id = @run_id AND severity = 'CRITICAL';
    SELECT @rules = STRING_AGG(CAST(CONCAT(source_table, '.', rule_name, ' (', n, ')') AS varchar(4000)), ', ')
    FROM (SELECT source_table, rule_name, COUNT(*) AS n FROM ops.dq_exceptions
          WHERE run_id = @run_id AND severity = 'CRITICAL' GROUP BY source_table, rule_name) r;
    SELECT @fails = COUNT(*) FROM ops.recon_results WHERE run_id = @run_id AND status = 'FAIL';
    SELECT @fail_list = STRING_AGG(CAST(CONCAT(check_name, ' [', dim_value, '] diff ', diff) AS varchar(4000)), '; ')
    FROM (SELECT TOP 10 * FROM ops.recon_results WHERE run_id = @run_id AND status = 'FAIL') f;
    SELECT @mape = holdout_mape FROM gold.agg_model_metrics;

    IF @critical > 0
        INSERT INTO ops.alert_log VALUES (CONCAT(@run_id, '-dq'), @run_id, SYSUTCDATETIME(), 'CRITICAL', 'DATA_QUALITY',
            CONCAT(@critical, ' record(s) quarantined: ', @rules, '. Pipeline continued; these rows are not in Gold or the model.'),
            CONCAT('Root cause: SELECT * FROM lh_marketing.dbo.silver_quarantine WHERE run_id = ''', @run_id,
                   '''; EXEC ops.usp_trace_week @region, @week_start for the affected rows'),
            @recipient, 'PENDING', NULL);
    IF @fails > 0
        INSERT INTO ops.alert_log VALUES (CONCAT(@run_id, '-recon'), @run_id, SYSUTCDATETIME(), 'CRITICAL',
            CASE WHEN EXISTS (SELECT 1 FROM ops.recon_results WHERE run_id = @run_id AND status = 'FAIL'
                              AND check_name = 'holdout MAPE <= threshold') THEN 'MODEL_DRIFT' ELSE 'RECONCILIATION_MISMATCH' END,
            CONCAT(@fails, ' check(s) failed: ', @fail_list, '. Holdout MAPE ', @mape),
            CONCAT('Root cause: SELECT * FROM ops.recon_results WHERE run_id = ''', @run_id, ''' AND status = ''FAIL'''),
            @recipient, 'PENDING', NULL);

    DECLARE @status varchar(20) = CASE WHEN @critical > 0 OR @fails > 0 THEN 'ALERT' ELSE 'OK' END;
    DECLARE @msg varchar(4000) = CONCAT('critical exceptions ', @critical, ', check failures ', @fails, ', holdout MAPE ', @mape);
    EXEC ops.usp_log_step @run_id, 'ops', 'reconcile', @status, NULL, NULL, @started, @msg;

    SELECT @run_id AS run_id, @status AS overall_status, @critical AS critical_exceptions, @fails AS recon_failures,
           (SELECT COUNT(*) FROM ops.recon_results WHERE run_id = @run_id) AS checks_run;
END;
GO

-- Root cause helper: one region-week through every layer and the model
CREATE OR ALTER PROCEDURE ops.usp_trace_week @region varchar(50), @week_start date
AS
BEGIN
    SELECT 'bronze media' AS layer, channel, COUNT(*) AS rows_, SUM(TRY_CAST(spend AS float)) AS spend
    FROM lh_marketing.dbo.bronze_media_spend
    WHERE region = @region AND TRY_CAST(date AS date) BETWEEN @week_start AND DATEADD(day, 6, @week_start)
    GROUP BY channel;
    SELECT 'silver media' AS layer, channel, COUNT(*) AS rows_, SUM(spend) AS spend
    FROM lh_marketing.dbo.fact_media_daily WHERE region = @region AND week_start = @week_start GROUP BY channel;
    SELECT 'quarantine' AS layer, source_table, failed_rules, raw_record FROM lh_marketing.dbo.silver_quarantine
    WHERE JSON_VALUE(raw_record, '$.region') = @region;
    SELECT 'gold media' AS layer, channel, spend, contribution FROM gold.fact_weekly_media
    WHERE region = @region AND week_start = @week_start;
    SELECT 'gold decomposition' AS layer, component, contribution FROM gold.fact_decomposition
    WHERE region = @region AND week_start = @week_start;
END;
GO
