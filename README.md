# nigeria-post-harvest-loss-analysis
An end-to-end agricultural data analytics project examining post-harvest crop losses, economic valuation coverage and geographic patterns in Nigeria using Python, MySQL and Power BI.



# The Cost of Post-Harvest Crop Losses in Nigeria

### An Agricultural Data Analytics and Business Intelligence Case Study

**Author:** Oluwatoyin Alfred Aderinwale

**Project Category:** Agricultural Economics | Data Analytics | Business Intelligence

**Tools:** Python | MySQL Workbench | Power BI | DAX

---

## 1. Project Overview

Post-harvest crop losses represent an important challenge within agricultural production and food supply chains. Understanding the scale, distribution and economic implications of reported losses can help inform better agricultural planning, infrastructure assessments and investment decisions.

This project examines reported post-harvest losses across selected agricultural commodities in Nigeria using 2023/24 household agricultural survey evidence, supported by separate national production, market and external benchmark datasets.

The project follows an end-to-end data analytics process covering data preparation, processing, analysis, visualization, reporting and proposed actions.

## 2. Business Problem

The central analytical question is:

**What do household agricultural survey data reveal about reported post-harvest crop losses, their economic valuation and the locations and crop categories that may require further investigation?**

The analysis focuses on:

- Comparing reported losses across selected crops.
- Examining geographic differences in observed crop-loss patterns.
- Evaluating the monetary valuation of reported losses.
- Assessing data quality, completeness and measurement limitations.
- Identifying potential areas for more detailed field assessment.

## 3. Data Sources and Scope

The main household survey evidence covers the 2023/24 observation period.

The analysis focuses on seven selected crops:

- Maize
- Cassava
- Yam
- Tomato
- Banana
- Rice
- Sorghum

Additional contextual datasets cover national production from 2014–2024, market features from 2023 and external loss benchmark observations from 2014–2021.

These datasets have different observation periods and measurement purposes. They are not treated as a single longitudinal household dataset.

**Main survey source:** [World Bank Microdata Library – Catalogue 6410](https://microdata.worldbank.org/catalog/6410)

Raw microdata is not included in this public repository. Users should consult the original source and its applicable access and usage conditions.

## 4. Tools and Technologies

| Tool | Application |
|---|---|
| Python | Data processing, cleaning, validation and analytical preparation |
| MySQL Workbench | Structured data storage and management |
| Power BI | Data modelling, interactive dashboards and reporting |
| DAX | Analytical measures and KPI calculations |

## 5. Analytical Workflow

The project followed these major stages:

**Data preparation:** Obtained and reviewed the source datasets, examined their structure and established the analytical scope.

**Data processing:** Developed a Python-based workflow to clean, transform, validate and prepare the datasets for analysis.

**Data management:** Imported the prepared analytical dataset into MySQL Workbench.

**Data analysis:** Examined crop-level loss patterns, weighted rates, geographic observations, monetary valuation and measurement coverage.

**Data visualization:** Developed a Power BI data model and dashboard to communicate the key findings.

**Reporting and action planning:** Prepared an executive presentation, documented limitations and proposed additional assessment activities.

## 6. Key Analytical Findings

The prepared analytical survey contained:

| Indicator | Result |
|---|---:|
| Crop records | 4,357 |
| Distinct households | 2,791 |
| Positive-loss records | 99 |
| Positively valued records | 72 |
| Unvalued positive-loss records | 27 |
| Valuation coverage | 72.73% |
| Covered sample loss value | ₦2,472,582.47 |

The monetary figure represents a nominal, unweighted and partially valued sample amount. It should not be interpreted as a national estimate of agricultural losses.

The analysis also identified differences in observed crop-level loss estimates and highlighted the importance of interpreting percentages alongside sample sizes and coverage.

Ekiti yam and Kogi cassava emerged as candidates for additional assessment, rather than definitive national loss hotspots.

## 7. Project Deliverables

The project produced:

- An automated Python data-processing workflow.
- Prepared analytical datasets and validation outputs.
- A structured data management workflow using MySQL.
- A Power BI analytical model and interactive dashboard.
- An executive summary and presentation.
- A proposed action and monitoring framework.

## 8. Limitations and Interpretation

The following limitations are important when interpreting the findings:

1. The household survey represents a 2023/24 cross-section, not an annual household-loss panel.
2. Crop-level loss estimates describe the eligible reporting subsets and observation period.
3. The financial valuation is nominal, unweighted and covers only 72 of 99 positive-loss records.
4. Twenty-seven positive-loss records remain without accepted monetary values.
5. The analysis does not establish intervention effectiveness, investment returns or a definitive national ranking of loss hotspots.

## 9. Repository Contents

This repository is intended to contain the original analytical code, methodology documentation, approved aggregated outputs, dashboard previews and executive reporting materials.

Restricted microdata, respondent-level records and the Power BI model containing individual crop records are deliberately excluded.

## 10. Conclusion

This project demonstrates how agricultural survey data can be transformed into structured analytical evidence to support agricultural economics research, business intelligence and evidence-based decision-making.

It also highlights the importance of data validation, transparent measurement, appropriate interpretation and the need for additional field evidence before making investment or intervention decisions.

---

**Author:** Oluwatoyin Alfred Aderinwale

**Project Focus:** Agricultural Economics, Data Analytics and Business Intelligence
:::
