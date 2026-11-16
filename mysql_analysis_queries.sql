/* ============================================================================
   THE COST OF POST-HARVEST CROP LOSSES IN NIGERIA
   Consolidated MySQL analysis queries

   Author: Oluwatoyin Alfred Aderinwale
   Database: agricultural_analysis
   Main analysis table: agricultural_analysis.analysis_survey_ready
   SQL dialect: MySQL 8.0+ (CTE and window-function support is used)

   PURPOSE
   -------
   This file brings the documented survey-analysis calculations and their
   reproducible supporting queries into one place. It covers data verification,
   survey coverage, weighted physical-loss rates, valuation, price sensitivity
   and the household-influence diagnostic.

   PROVENANCE NOTE
   ---------------
   The Phase 4 Analysis report contains three representative SQL statements
   verbatim: the overall record/household count, the crop-condition weighted
   loss rate, and the crop-condition valuation calculation. Those three are
   retained below in the same analytical form. The other queries are clearly
   documented, reproducible extensions of the final methods and output
   definitions recorded for the project; they are not represented as a verbatim
   export of the original MySQL Workbench query-history window.

   IMPORTANT
   ---------
   1. This script is SELECT-only. It does not create, update or delete data.
   2. It assumes the trusted, corrected table is named
      agricultural_analysis.analysis_survey_ready.
   3. Earlier incomplete imports and the large raw SQL import/dump are not used.
   4. Keep the household-level source table and Power BI model private. Do not
      publish row-level output, household identifiers or coordinates to GitHub.
   5. The core survey table is the source for household survey analysis. National
      production, market-price context and external loss benchmarks were prepared
      as separate pipeline outputs and are not joined to household rows here.

   RUNNING THE FILE
   ----------------
   Open this file in MySQL Workbench. Confirm the schema and table name, then
   execute the queries section by section. Run the data-quality checks before
   interpreting the analytical results.
   ============================================================================ */

USE agricultural_analysis;

/* --------------------------------------------------------------------------
   SECTION 00. CONFIRM THE DATABASE AND TABLE
   -------------------------------------------------------------------------- */
SELECT DATABASE() AS active_database;

SHOW COLUMNS FROM analysis_survey_ready;

/* --------------------------------------------------------------------------
   SECTION 01. OVERALL DATASET PROFILE
   Documented representative query from the Phase 4 Analysis appendix.
   Expected project snapshot: 4,357 crop records and 2,791 distinct households.
   -------------------------------------------------------------------------- */
SELECT
    COUNT(*) AS crop_records,
    COUNT(DISTINCT hhid) AS households
FROM agricultural_analysis.analysis_survey_ready;

/* --------------------------------------------------------------------------
   SECTION 02. DATA COMPLETENESS AND MEASUREMENT COVERAGE
   Counts missing values without replacing NULL with zero.
   -------------------------------------------------------------------------- */
SELECT
    COUNT(*) AS total_crop_records,
    COUNT(*) - COUNT(harvest_kg) AS missing_harvest_kg,
    COUNT(*) - COUNT(lost_kg) AS missing_lost_kg,
    COUNT(*) - COUNT(estimated_loss_value_ngn) AS missing_estimated_loss_value_ngn,
    SUM(CASE WHEN valid_kg_loss_measure = 'True' THEN 1 ELSE 0 END)
        AS flagged_valid_kg_records,
    SUM(CASE WHEN survey_weight > 0
                  AND harvest_kg > 0
                  AND lost_kg IS NOT NULL
                  AND lost_kg BETWEEN 0 AND harvest_kg
             THEN 1 ELSE 0 END) AS records_meeting_numeric_kg_rules
FROM agricultural_analysis.analysis_survey_ready;

/* Positive-loss valuation coverage. The denominator is positive-loss records,
   not all crop records. */
SELECT
    COUNT(CASE WHEN lost_native_units > 0 THEN 1 END)
        AS positive_loss_records,
    COUNT(CASE WHEN lost_native_units > 0
                    AND estimated_loss_value_ngn IS NOT NULL
               THEN 1 END) AS positive_loss_valued_records,
    COUNT(CASE WHEN lost_native_units > 0
                    AND estimated_loss_value_ngn IS NULL
               THEN 1 END) AS positive_loss_unvalued_records,
    ROUND(
        100.0 * COUNT(CASE WHEN lost_native_units > 0
                                AND estimated_loss_value_ngn IS NOT NULL
                           THEN 1 END)
        / NULLIF(COUNT(CASE WHEN lost_native_units > 0 THEN 1 END), 0),
        2
    ) AS positive_loss_valuation_coverage_pct
FROM agricultural_analysis.analysis_survey_ready;

/* --------------------------------------------------------------------------
   SECTION 03. ELIGIBILITY PROFILE FOR KILOGRAM-BASED LOSS ANALYSIS
   The same eligibility rules are used for the weighted lost-mass and
   weighted harvested-mass denominators.
   -------------------------------------------------------------------------- */
SELECT
    COUNT(*) AS all_focus_crop_records,
    SUM(CASE WHEN valid_kg_loss_measure = 'True'
                  AND survey_weight > 0
                  AND harvest_kg > 0
                  AND lost_kg IS NOT NULL
                  AND lost_kg BETWEEN 0 AND harvest_kg
             THEN 1 ELSE 0 END) AS eligible_kg_records,
    SUM(CASE WHEN NOT (
                    valid_kg_loss_measure = 'True'
                    AND survey_weight > 0
                    AND harvest_kg > 0
                    AND lost_kg IS NOT NULL
                    AND lost_kg BETWEEN 0 AND harvest_kg
                 ) OR valid_kg_loss_measure IS NULL
             THEN 1 ELSE 0 END) AS records_outside_kg_analysis
FROM agricultural_analysis.analysis_survey_ready;

/* --------------------------------------------------------------------------
   SECTION 04. SURVEY-GRAIN DUPLICATE CHECK
   The project documentation identifies household, crop code and survey season
   as the crop-disposition key. This query lists any repeated combinations for
   review; it does not delete or automatically deduplicate records.
   -------------------------------------------------------------------------- */
SELECT
    hhid,
    crop_code,
    survey_season,
    COUNT(*) AS records_with_same_key
FROM agricultural_analysis.analysis_survey_ready
GROUP BY hhid, crop_code, survey_season
HAVING COUNT(*) > 1
ORDER BY records_with_same_key DESC, hhid, crop_code;

/* --------------------------------------------------------------------------
   SECTION 05. CROP- AND CONDITION-LEVEL WEIGHTED PHYSICAL-LOSS RATE
   Documented representative query from the Phase 4 Analysis appendix.

   Formula:
     100 * SUM(survey_weight * lost_kg)
         / SUM(survey_weight * harvest_kg)

   Confirmed zero-loss records remain in the eligible denominator.
   -------------------------------------------------------------------------- */
SELECT
    commodity,
    condition_group,
    COUNT(*) AS eligible_crop_records,
    COUNT(DISTINCT hhid) AS eligible_households,
    SUM(CASE WHEN lost_kg > 0 THEN 1 ELSE 0 END)
        AS positive_loss_kg_records,
    ROUND(
        100.0 * SUM(survey_weight * lost_kg)
        / NULLIF(SUM(survey_weight * harvest_kg), 0),
        4
    ) AS weighted_loss_rate_pct
FROM agricultural_analysis.analysis_survey_ready
WHERE valid_kg_loss_measure = 'True'
  AND survey_weight > 0
  AND harvest_kg > 0
  AND lost_kg IS NOT NULL
  AND lost_kg BETWEEN 0 AND harvest_kg
GROUP BY commodity, condition_group
ORDER BY weighted_loss_rate_pct DESC;

/* --------------------------------------------------------------------------
   SECTION 06. ZONE-, CROP- AND CONDITION-LEVEL WEIGHTED LOSS RATE
   Same denominator and eligibility rules as Section 05.
   -------------------------------------------------------------------------- */
SELECT
    zone,
    commodity,
    condition_group,
    COUNT(*) AS eligible_crop_records,
    COUNT(DISTINCT hhid) AS eligible_households,
    COUNT(DISTINCT CASE WHEN lost_kg > 0 THEN hhid END)
        AS positive_loss_households,
    SUM(CASE WHEN lost_kg > 0 THEN 1 ELSE 0 END)
        AS positive_loss_crop_records,
    ROUND(
        100.0 * SUM(survey_weight * lost_kg)
        / NULLIF(SUM(survey_weight * harvest_kg), 0),
        4
    ) AS weighted_loss_rate_pct
FROM agricultural_analysis.analysis_survey_ready
WHERE valid_kg_loss_measure = 'True'
  AND survey_weight > 0
  AND harvest_kg > 0
  AND lost_kg IS NOT NULL
  AND lost_kg BETWEEN 0 AND harvest_kg
GROUP BY zone, commodity, condition_group
ORDER BY weighted_loss_rate_pct DESC, zone, commodity, condition_group;

/* --------------------------------------------------------------------------
   SECTION 07. STATE-, CROP- AND CONDITION-LEVEL WEIGHTED LOSS RATE
   The fewer-than-30-household indicator is a descriptive coverage flag only;
   it is not a statistical reliability test.
   -------------------------------------------------------------------------- */
SELECT
    state,
    commodity,
    condition_group,
    COUNT(*) AS eligible_crop_records,
    COUNT(DISTINCT hhid) AS eligible_households,
    COUNT(DISTINCT CASE WHEN lost_kg > 0 THEN hhid END)
        AS positive_loss_households,
    SUM(CASE WHEN lost_kg > 0 THEN 1 ELSE 0 END)
        AS positive_loss_crop_records,
    ROUND(
        100.0 * SUM(survey_weight * lost_kg)
        / NULLIF(SUM(survey_weight * harvest_kg), 0),
        4
    ) AS weighted_loss_rate_pct,
    CASE WHEN COUNT(DISTINCT hhid) < 30 THEN 'Fewer than 30 households'
         ELSE '30 or more households'
    END AS descriptive_sample_flag
FROM agricultural_analysis.analysis_survey_ready
WHERE valid_kg_loss_measure = 'True'
  AND survey_weight > 0
  AND harvest_kg > 0
  AND lost_kg IS NOT NULL
  AND lost_kg BETWEEN 0 AND harvest_kg
GROUP BY state, commodity, condition_group
ORDER BY weighted_loss_rate_pct DESC, state, commodity, condition_group;

/* --------------------------------------------------------------------------
   SECTION 08. CROP- AND CONDITION-LEVEL FINANCIAL VALUATION
   Documented representative query from the Phase 4 Analysis appendix.

   Important treatment:
   - A group with no positive-loss records returns zero.
   - A group with positive-loss records but no accepted value returns NULL.
   - Missing positive-loss values are not silently converted to zero.
   -------------------------------------------------------------------------- */
SELECT
    commodity,
    condition_group,
    COUNT(CASE WHEN lost_native_units > 0 THEN 1 END)
        AS positive_loss_records,
    COUNT(CASE WHEN lost_native_units > 0
                     AND estimated_loss_value_ngn IS NOT NULL
                THEN 1 END) AS positive_loss_valued,
    COUNT(CASE WHEN lost_native_units > 0
                     AND estimated_loss_value_ngn IS NULL
                THEN 1 END) AS positive_loss_unvalued,
    CASE WHEN COUNT(CASE WHEN lost_native_units > 0
                          THEN 1 END) = 0
         THEN 0
         ELSE ROUND(SUM(CASE WHEN lost_native_units > 0
                             THEN estimated_loss_value_ngn END), 2)
    END AS covered_sample_loss_ngn
FROM agricultural_analysis.analysis_survey_ready
GROUP BY commodity, condition_group
ORDER BY commodity, condition_group;

/* --------------------------------------------------------------------------
   SECTION 09. STATE-, CROP- AND CONDITION-LEVEL FINANCIAL VALUATION
   Values are nominal and unweighted. They are not state-level population
   loss estimates.
   -------------------------------------------------------------------------- */
SELECT
    state,
    commodity,
    condition_group,
    COUNT(CASE WHEN lost_native_units > 0 THEN 1 END)
        AS positive_loss_records,
    COUNT(CASE WHEN lost_native_units > 0
                    AND estimated_loss_value_ngn IS NOT NULL
               THEN 1 END) AS positive_loss_valued_records,
    COUNT(CASE WHEN lost_native_units > 0
                    AND estimated_loss_value_ngn IS NULL
               THEN 1 END) AS positive_loss_unvalued_records,
    CASE WHEN COUNT(CASE WHEN lost_native_units > 0 THEN 1 END) = 0
         THEN 0
         ELSE ROUND(SUM(CASE WHEN lost_native_units > 0
                             THEN estimated_loss_value_ngn END), 2)
    END AS covered_sample_loss_ngn
FROM agricultural_analysis.analysis_survey_ready
GROUP BY state, commodity, condition_group
HAVING COUNT(CASE WHEN lost_native_units > 0 THEN 1 END) > 0
ORDER BY covered_sample_loss_ngn DESC, state, commodity;

/* --------------------------------------------------------------------------
   SECTION 10. VALUATION METHOD REVIEW
   Shows positive-loss records and covered values by valuation method.
   NULL method labels are explicitly retained as unavailable/unvalued.
   -------------------------------------------------------------------------- */
SELECT
    commodity,
    condition_group,
    COALESCE(valuation_method, 'UNVALUED / NO ACCEPTED METHOD')
        AS valuation_method_group,
    COUNT(*) AS positive_loss_records_in_method_group,
    COUNT(estimated_loss_value_ngn) AS records_with_accepted_value,
    COUNT(*) - COUNT(estimated_loss_value_ngn) AS records_without_accepted_value,
    ROUND(SUM(estimated_loss_value_ngn), 2) AS covered_sample_loss_ngn
FROM agricultural_analysis.analysis_survey_ready
WHERE lost_native_units > 0
GROUP BY commodity,
         condition_group,
         COALESCE(valuation_method, 'UNVALUED / NO ACCEPTED METHOD')
ORDER BY commodity, condition_group, valuation_method_group;

/* --------------------------------------------------------------------------
   SECTION 11. PAIRED PRICE-SENSITIVITY CHECK
   Includes records with both own-sales and external-proxy loss valuations.
   Percentage difference uses the own-sales estimate as the comparison base:

     100 * (external proxy value - own-sales value) / own-sales value

   The project report records only three paired observations. This is a small
   diagnostic and should not be generalised into a claim of systematic price bias.
   -------------------------------------------------------------------------- */
SELECT
    hhid,
    commodity,
    condition_group,
    state,
    own_unit_value_loss_ngn AS own_sales_loss_value_ngn,
    external_proxy_loss_value_ngn,
    ROUND(
        external_proxy_loss_value_ngn - own_unit_value_loss_ngn,
        2
    ) AS external_minus_own_ngn,
    ROUND(
        100.0 * (external_proxy_loss_value_ngn - own_unit_value_loss_ngn)
        / NULLIF(own_unit_value_loss_ngn, 0),
        2
    ) AS external_vs_own_difference_pct,
    sale_year,
    price_reference_year,
    external_proxy_method
FROM agricultural_analysis.analysis_survey_ready
WHERE lost_native_units > 0
  AND own_unit_value_loss_ngn IS NOT NULL
  AND external_proxy_loss_value_ngn IS NOT NULL
  AND own_unit_value_loss_ngn <> 0
ORDER BY commodity, state, hhid;

/* --------------------------------------------------------------------------
   SECTION 12. HOUSEHOLD-INFLUENCE / LEAVE-LARGEST-CONTRIBUTOR DIAGNOSTIC

   Method:
   1. Retain the exact eligible kilogram records used for the main loss rate.
   2. Sum each household's weighted lost and harvested kilograms within each
      state x crop x condition group (all variety rows for that household stay
      together).
   3. Rank households by their contribution to weighted lost mass.
   4. Remove the largest contributor from both weighted sums and recalculate
      the rate.

   This diagnostic checks concentration; it is not a confidence interval,
   statistical significance test or causal estimate. The household identifier
   is used internally for ranking but is not returned in the result.
   Requires MySQL 8.0+.
   -------------------------------------------------------------------------- */
WITH eligible_mass_records AS (
    SELECT
        hhid,
        state,
        commodity,
        condition_group,
        survey_weight,
        harvest_kg,
        lost_kg
    FROM agricultural_analysis.analysis_survey_ready
    WHERE valid_kg_loss_measure = 'True'
      AND survey_weight > 0
      AND harvest_kg > 0
      AND lost_kg IS NOT NULL
      AND lost_kg BETWEEN 0 AND harvest_kg
),
household_contributions AS (
    SELECT
        state,
        commodity,
        condition_group,
        hhid,
        COUNT(*) AS household_crop_records,
        SUM(survey_weight * lost_kg) AS household_weighted_lost_kg,
        SUM(survey_weight * harvest_kg) AS household_weighted_harvest_kg,
        MAX(CASE WHEN lost_kg > 0 THEN 1 ELSE 0 END)
            AS household_has_positive_loss
    FROM eligible_mass_records
    GROUP BY state, commodity, condition_group, hhid
),
group_totals AS (
    SELECT
        state,
        commodity,
        condition_group,
        SUM(household_crop_records) AS eligible_crop_records,
        COUNT(*) AS eligible_households,
        SUM(household_has_positive_loss) AS positive_loss_households,
        SUM(household_weighted_lost_kg) AS total_weighted_lost_kg,
        SUM(household_weighted_harvest_kg) AS total_weighted_harvest_kg
    FROM household_contributions
    GROUP BY state, commodity, condition_group
),
ranked_households AS (
    SELECT
        state,
        commodity,
        condition_group,
        household_weighted_lost_kg,
        household_weighted_harvest_kg,
        ROW_NUMBER() OVER (
            PARTITION BY state, commodity, condition_group
            ORDER BY household_weighted_lost_kg DESC, hhid ASC
        ) AS contribution_rank
    FROM household_contributions
)
SELECT
    g.state,
    g.commodity,
    g.condition_group,
    g.eligible_crop_records,
    g.eligible_households,
    g.positive_loss_households,
    ROUND(
        100.0 * g.total_weighted_lost_kg
        / NULLIF(g.total_weighted_harvest_kg, 0),
        4
    ) AS full_sample_weighted_loss_rate_pct,
    ROUND(r.household_weighted_lost_kg, 4)
        AS largest_household_weighted_lost_kg,
    ROUND(
        100.0 * (g.total_weighted_lost_kg - r.household_weighted_lost_kg)
        / NULLIF(
            g.total_weighted_harvest_kg - r.household_weighted_harvest_kg,
            0
        ),
        4
    ) AS rate_after_removing_largest_contributor_pct
FROM group_totals AS g
JOIN ranked_households AS r
  ON r.state = g.state
 AND r.commodity = g.commodity
 AND r.condition_group = g.condition_group
 AND r.contribution_rank = 1
WHERE g.total_weighted_lost_kg > 0
ORDER BY g.state, g.commodity, g.condition_group;

/* --------------------------------------------------------------------------
   SECTION 13. FOLLOW-UP LEADS: EKITI YAM AND KOGI CASSAVA
   These are investigation leads from the observed survey subset, not a
   definitive national ranking or proof of a particular loss mechanism.
   -------------------------------------------------------------------------- */
SELECT
    state,
    commodity,
    condition_group,
    COUNT(*) AS eligible_crop_records,
    COUNT(DISTINCT hhid) AS eligible_households,
    COUNT(DISTINCT CASE WHEN lost_kg > 0 THEN hhid END)
        AS positive_loss_households,
    ROUND(
        100.0 * SUM(survey_weight * lost_kg)
        / NULLIF(SUM(survey_weight * harvest_kg), 0),
        4
    ) AS weighted_loss_rate_pct
FROM agricultural_analysis.analysis_survey_ready
WHERE valid_kg_loss_measure = 'True'
  AND survey_weight > 0
  AND harvest_kg > 0
  AND lost_kg IS NOT NULL
  AND lost_kg BETWEEN 0 AND harvest_kg
  AND (
        (LOWER(state) = 'ekiti' AND LOWER(commodity) = 'yam')
        OR
        (LOWER(state) = 'kogi' AND LOWER(commodity) = 'cassava')
      )
GROUP BY state, commodity, condition_group
ORDER BY state, commodity, condition_group;

/* --------------------------------------------------------------------------
   SECTION 14. SMALL-GROUP REVIEW
   Descriptive screen only. Groups below 30 households should be interpreted
   with their eligible and positive-loss counts; this is not a formal precision
   or statistical-significance test.
   -------------------------------------------------------------------------- */
SELECT
    state,
    commodity,
    condition_group,
    COUNT(*) AS eligible_crop_records,
    COUNT(DISTINCT hhid) AS eligible_households,
    COUNT(DISTINCT CASE WHEN lost_kg > 0 THEN hhid END)
        AS positive_loss_households,
    ROUND(
        100.0 * SUM(survey_weight * lost_kg)
        / NULLIF(SUM(survey_weight * harvest_kg), 0),
        4
    ) AS weighted_loss_rate_pct
FROM agricultural_analysis.analysis_survey_ready
WHERE valid_kg_loss_measure = 'True'
  AND survey_weight > 0
  AND harvest_kg > 0
  AND lost_kg IS NOT NULL
  AND lost_kg BETWEEN 0 AND harvest_kg
GROUP BY state, commodity, condition_group
HAVING COUNT(DISTINCT hhid) < 30
ORDER BY eligible_households, weighted_loss_rate_pct DESC;

/* --------------------------------------------------------------------------
   SECTION 15. POSITIVE-LOSS RECORDS WITHOUT AN ACCEPTED FINANCIAL VALUE
   Returns record-level information for private quality review only.
   Do not publish this result or its household identifiers in a public repo.
   -------------------------------------------------------------------------- */
SELECT
    hhid,
    commodity,
    condition_group,
    state,
    lost_native_units,
    harvest_unit_label,
    valuation_method,
    own_unit_value_ngn_per_native_unit,
    external_proxy_price_ngn_per_kg,
    estimated_loss_value_ngn,
    positive_loss_unvalued
FROM agricultural_analysis.analysis_survey_ready
WHERE lost_native_units > 0
  AND estimated_loss_value_ngn IS NULL
ORDER BY commodity, state;

/* ============================================================================
   END OF CONSOLIDATED MYSQL ANALYSIS QUERIES

   SCOPE NOTE
   ----------
   The following contextual outputs are part of the wider project but are not
   reproduced by joining them to the household survey table in this script:
   - national production and yield (2014-2024)
   - monthly market-price features (2023 reporting slice, with historical
     baseline observations)
   - external loss benchmarks (recorded observations, 2014-2021)
   - one-point climate context

   Those are separate-grain pipeline outputs. Combining their rows with
   household crop records would risk duplicating survey observations or mixing
   incompatible units, periods, products and definitions. Keep them separate
   unless the corresponding table schemas and a defensible relationship have
   been explicitly established.
   ============================================================================ */
