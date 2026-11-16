"""Single-file Nigerian agricultural project pipeline.

Install the two dependencies once:
    python -m pip install pandas numpy

Run against the original ZIP:
    python AgricProject_Single_File.py --input "C:/Users/USER/Downloads/AgricProject_Datasets.zip"

Or run against the extracted dataset folder:
    python AgricProject_Single_File.py --input "C:/Users/USER/Downloads/Agric DA Project"

An optional --output argument selects a new/empty output directory.
The default creates results/run_YYYYMMDD_HHMMSS in the terminal's current folder.
Configuration defaults are embedded below; --config can optionally read a JSON
file. No project modules, requirements file, or separate configuration file are
needed. The original data archive is read without changing its contents.

The complete processing logic and 21 CSV exports match the modular version.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from collections.abc import Sequence
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from contextlib import contextmanager
import hashlib
from io import BytesIO
from zipfile import ZipFile
import re
import unicodedata
from io import StringIO
from datetime import datetime, timezone
import logging
import platform
import time
import argparse
from datetime import datetime

__version__ = "1.0.0"


# ========================================================================
# Configuration
# ========================================================================

@dataclass(frozen=True)
class Config:
    start_year: int = 2014
    end_year: int = 2026
    survey_season: str = "2023/24"
    survey_price_year: int = 2023
    target_commodities: tuple[str, ...] = (
        "maize", "cassava", "yam", "tomato", "banana", "rice", "sorghum"
    )
    minimum_price_months: int = 6
    rolling_months: int = 12
    minimum_rolling_months: int = 9
    spike_ratio: float = 1.5
    wfp_price_flags: tuple[str, ...] = ("actual", "actual,aggregate")
    allow_retail_valuation: bool = False
    low_sample_households: int = 30

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
        for key in ("target_commodities", "wfp_price_flags"):
            if key in data:
                data[key] = tuple(data[key])
        result = cls(**data)
        if not result.start_year <= result.survey_price_year <= result.end_year:
            raise ValueError("survey_price_year must be inside the requested period")
        if not 1 <= result.minimum_price_months <= 12:
            raise ValueError("minimum_price_months must be 1 to 12")
        if not 2 <= result.minimum_rolling_months <= result.rolling_months:
            raise ValueError("minimum_rolling_months must be 2 to rolling_months")
        if result.spike_ratio <= 0 or result.low_sample_households < 1:
            raise ValueError("spike_ratio and low_sample_households must be positive")
        if not result.target_commodities:
            raise ValueError("target_commodities cannot be empty")
        return result

    def as_dict(self) -> dict:
        return asdict(self)


# ========================================================================
# Validation and diagnostics
# ========================================================================

class DataQualityError(ValueError):
    pass


def require_columns(df: pd.DataFrame, columns: Sequence[str], name: str) -> None:
    if not df.columns.is_unique:
        raise DataQualityError(f"{name}: duplicate column names")
    absent = sorted(set(columns) - set(df.columns))
    if absent:
        raise DataQualityError(f"{name}: required columns absent: {absent}")


def require_unique(df: pd.DataFrame, keys: Sequence[str], name: str) -> None:
    require_columns(df, keys, name)
    if df[list(keys)].isna().any(axis=None):
        raise DataQualityError(f"{name}: missing primary keys")
    count = int(df.duplicated(list(keys), keep=False).sum())
    if count:
        raise DataQualityError(f"{name}: {count} rows share primary keys {list(keys)}")


def numeric(values: pd.Series) -> pd.Series:
    result = pd.to_numeric(values, errors="coerce").astype(float)
    return result.where(np.isfinite(result))


def safe_ratio(a: pd.Series, b: pd.Series) -> pd.Series:
    return a.div(b.where(b.gt(0)))


@dataclass
class Quality:
    metrics: dict = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    joins: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)

    def metric(self, name: str, value) -> None:
        self.metrics[name] = value.item() if isinstance(value, np.generic) else value

    def issue(self, code: str, message: str, **context) -> None:
        self.issues.append({"code": code, "message": message, **context})

    def export(self, path: Path) -> None:
        path.write_text(json.dumps(self.__dict__, indent=2, allow_nan=False), encoding="utf-8")


def left_join(
    left: pd.DataFrame, right: pd.DataFrame, keys: list[str], name: str,
    quality: Quality, *, minimum_match: float = 0.0,
) -> pd.DataFrame:
    """Attach a unique lookup; null keys on the left stay explicitly unmatched."""
    require_columns(left, keys, name + "/left")
    require_unique(right, keys, name + "/right")
    overlap = (set(left.columns) & set(right.columns)) - set(keys)
    if overlap:
        raise DataQualityError(f"{name}: unresolved column overlap {sorted(overlap)}")
    result = left.merge(right, how="left", on=keys, validate="many_to_one", indicator="__match")
    if len(result) != len(left):
        raise DataQualityError(f"{name}: unexpected row multiplication")
    matched = result["__match"].eq("both")
    rate = float(matched.mean()) if len(result) else None
    quality.joins.append({
        "name": name, "keys": keys, "left_rows": len(left), "right_rows": len(right),
        "output_rows": len(result), "matched_rows": int(matched.sum()),
        "left_rows_with_missing_keys": int(left[keys].isna().any(axis=1).sum()),
        "key_match_rate": rate,
    })
    if len(result) and rate < minimum_match:
        raise DataQualityError(f"{name}: match rate {rate:.1%} below {minimum_match:.1%}")
    return result.drop(columns="__match")


# ========================================================================
# Archive and folder readers
# ========================================================================

class Sources:
    """Read the user's outer ZIP or an extracted folder, including nested ZIPs.

    Inputs are never extracted over user files. Each used file's exact bytes are
    hashed. Nested archives remain in memory and are closed at the end of a run.
    """

    def __init__(self, location: Path, quality: Quality):
        self.location = location.resolve()
        self.quality = quality
        self._outer = ZipFile(location) if location.is_file() and location.suffix.lower() == ".zip" else None
        if self._outer:
            self._files = {name: name for name in self._outer.namelist() if not name.endswith("/")}
        elif location.is_dir():
            self._files = {p.relative_to(location).as_posix(): p for p in location.rglob("*") if p.is_file()}
        else:
            raise FileNotFoundError("--input must be the dataset ZIP or an extracted data directory")
        self._nested: dict[str, ZipFile] = {}
        self._recorded: set[str] = set()

    def close(self):
        for archive in self._nested.values():
            archive.close()
        if self._outer:
            self._outer.close()

    def find(self, basename: str) -> str:
        found = [name for name in self._files if Path(name).name.lower() == basename.lower()]
        if len(found) != 1:
            raise DataQualityError(f"Expected one {basename}; found {len(found)}")
        return found[0]

    @contextmanager
    def open(self, name: str):
        if self._outer:
            with self._outer.open(name) as stream:
                yield stream
        else:
            with self._files[name].open("rb") as stream:
                yield stream

    def bytes(self, name: str) -> bytes:
        with self.open(name) as stream:
            return stream.read()

    def nested(self, basename: str) -> ZipFile:
        if basename not in self._nested:
            archive_name = self.find(basename)
            content = self.bytes(archive_name)
            self.record(archive_name, content)
            self._nested[basename] = ZipFile(BytesIO(content))
        return self._nested[basename]

    def record(self, name: str, content: bytes, frame: pd.DataFrame | None = None):
        if name in self._recorded:
            return
        self._recorded.add(name)
        item = {"name": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
        if frame is not None:
            item.update({"rows": len(frame), "columns": frame.columns.tolist()})
        self.quality.sources.append(item)

    def csv(self, basename: str, *, archive: str | None = None, **kwargs) -> pd.DataFrame:
        if archive:
            z = self.nested(archive)
            names = [n for n in z.namelist() if Path(n).name.lower() == basename.lower()]
            if len(names) != 1:
                raise DataQualityError(f"Expected one {basename} inside {archive}; found {len(names)}")
            content = z.read(names[0])
            identity = archive + "!" + names[0]
        else:
            identity = self.find(basename)
            content = self.bytes(identity)
        frame = pd.read_csv(BytesIO(content), dtype="string", encoding="utf-8-sig", **kwargs)
        frame.columns = frame.columns.str.strip().str.lower()
        if not frame.columns.is_unique:
            raise DataQualityError(f"{identity}: duplicate normalized column names")
        self.record(identity, content, frame)
        return frame

    def archive_inventory(self, basename: str) -> list[dict]:
        archive = self.nested(basename)
        result = []
        for member in archive.infolist():
            if member.is_dir():
                continue
            item = {"archive": basename, "member": member.filename, "bytes": member.file_size}
            if member.filename.lower().endswith(".csv"):
                with archive.open(member) as stream:
                    columns = pd.read_csv(stream, nrows=0).columns.tolist()
                item["columns"] = columns
                item["contains_cropcode"] = "cropcode" in columns
                item["contains_plotid"] = "plotid" in columns
            result.append(item)
        return result


# ========================================================================
# Explicit source mappings
# ========================================================================

SURVEY_CROPS = {
    1010: "cowpea", 1020: "cassava", 1040: "cocoyam", 1050: "cotton",
    1060: "groundnut", 1070: "sorghum", 1080: "maize", 1090: "melonseed",
    1100: "millet", 1110: "rice", 1121: "yam", 1122: "yam", 1123: "yam", 1124: "yam",
    2010: "fonio", 2020: "bambara_groundnut", 2030: "banana", 2040: "sesame",
    2050: "carrot", 2060: "cucumber", 2070: "cabbage", 2080: "garden_egg",
    2100: "ginger", 2120: "okra", 2130: "onion", 2141: "sweet_pepper", 2142: "hot_pepper",
    2150: "pigeon_pea", 2160: "pineapple", 2170: "plantain", 2180: "potato",
    2181: "sweet_potato", 2190: "pumpkin", 2194: "leafy_vegetables", 2220: "soybean",
    2230: "sugarcane", 2260: "tomato", 2290: "roselle", 3020: "cashew", 3030: "chilli",
    3040: "cocoa", 3050: "coconut", 3090: "guava", 3110: "kola_nut", 3160: "mango",
    3170: "orange", 3180: "oil_palm_fruit", 3190: "ogbono", 3210: "papaya",
    3220: "pear", 3221: "avocado",
}


def codes(values: pd.Series) -> pd.Series:
    """Parse numeric codes and labelled exports, including '1080. MAIZE'."""
    text = values.astype("string").str.strip()
    result = pd.to_numeric(text, errors="coerce")
    labelled = pd.to_numeric(text.str.extract(r"^([0-9]+)\.\s+", expand=False), errors="coerce")
    result = result.fillna(labelled)
    return result.where(result.mod(1).eq(0)).astype("Int64")


def labels(values: pd.Series) -> pd.Series:
    return values.astype("string").str.strip().str.replace(r"^[0-9]+\.\s+", "", regex=True)


def identifier(values: pd.Series) -> pd.Series:
    result = values.astype("string").str.strip().str.replace(r"\.0+$", "", regex=True)
    return result.mask(result.str.lower().isin(["", "nan", "none", "null", "<na>"]))


def normalise_place(value):
    if pd.isna(value):
        return pd.NA
    text = re.sub(r"^[0-9]+\.\s+", "", str(value).strip())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    # Exact aliases only; no fuzzy or nearest-place matching.
    aliases = {
        "fct": "federal capital territory", "abuja": "federal capital territory",
        "f c t": "federal capital territory", "abuja fct": "federal capital territory",
        "fct abuja": "federal capital territory",
    }
    return aliases.get(text, text) if text else pd.NA


# Each market label keeps its own variant; the family groups below support
# explicitly labelled regional proxy prices, not grade-equivalent transactions.
MARKET_PRODUCTS = {
    "maize": ("maize", "maize_grain"),
    "maize (white)": ("maize", "maize_grain"),
    "maize (yellow)": ("maize", "maize_grain"),
    "maize grain (white)": ("maize", "maize_grain"),
    "maize grain (yellow)": ("maize", "maize_grain"),
    "maize flour": ("maize", "maize_flour"),
    "sorghum": ("sorghum", "sorghum_grain"),
    "sorghum (white)": ("sorghum", "sorghum_grain"),
    "sorghum (brown)": ("sorghum", "sorghum_grain"),
    "millet": ("millet", "millet_grain"),
    "millet (pearl)": ("millet", "millet_grain"),
    "tomatoes": ("tomato", "tomato_fresh"),
    "bananas": ("banana", "banana_fresh"),
    "yam": ("yam", "yam_fresh"), "yam (abuja)": ("yam", "yam_fresh"),
    "yams": ("yam", "yam_fresh"),
    "rice (local)": ("rice", "rice_market_unspecified"),
    "rice (milled, local)": ("rice", "rice_milled_local"),
    "rice (milled)": ("rice", "rice_milled_local"),
    "rice (imported)": ("rice", "rice_imported"),
    "rice (5% broken)": ("rice", "rice_imported"),
    "gari (white)": ("cassava", "cassava_gari"),
    "gari (yellow)": ("cassava", "cassava_gari"),
    "cassava meal (gari, yellow)": ("cassava", "cassava_gari"),
    "beans (niebe)": ("cowpea", "cowpea_grain"),
    "cowpeas": ("cowpea", "cowpea_grain"),
    "cowpeas (white)": ("cowpea", "cowpea_grain"),
    "cowpeas (brown)": ("cowpea", "cowpea_grain"),
    "groundnuts (shelled)": ("groundnut", "groundnut_shelled"),
    "groundnuts": ("groundnut", "groundnut_unspecified"),
    "onions": ("onion", "onion_fresh"), "oranges": ("orange", "orange_fresh"),
    "spinach": ("leafy_vegetables", "leafy_vegetables_fresh"),
    "watermelons": ("watermelon", "watermelon_fresh"),
    "wheat": ("wheat", "wheat_grain"),
    "oil (palm)": ("oil_palm_fruit", "palm_oil"),
    "palm oil (refined)": ("oil_palm_fruit", "palm_oil"),
}

FAO_PRODUCTS = {
    "maize (corn)": "maize", "cassava, fresh": "cassava", "yams": "yam",
    "tomatoes": "tomato", "bananas": "banana", "rice": "rice", "sorghum": "sorghum",
    "millet": "millet", "cow peas, dry": "cowpea", "cassava": "cassava",
    "cassava, dry": "cassava", "flour of cassava": "cassava",
    "onions": "onion", "sweet potatoes": "sweet_potato",
}


def mass_kg(unit) -> float | None:
    """Recognise explicit mass units only; tubers, litres and pieces stay unknown."""
    if pd.isna(unit):
        return None
    text = str(unit).strip().lower().replace("_", " ")
    match = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*)?(kg|g)", text)
    if not match:
        return None
    quantity = float(match.group(1) or 1)
    return quantity if match.group(2) == "kg" else quantity / 1000


def survey_market_group(commodity: str, condition):
    fresh = {"tomato": "tomato_fresh", "banana": "banana_fresh", "yam": "yam_fresh"}
    if commodity in fresh:
        return fresh[commodity]
    # Unshelled maize/sorghum are not priced as clean grain. Rice condition does
    # not establish milled-rice equivalence, and gari is not fresh cassava.
    if commodity in {"maize", "sorghum", "millet", "cowpea"} and pd.notna(condition) and condition == 2:
        return commodity + "_grain"
    return pd.NA


# ========================================================================
# Survey processing
# ========================================================================

WAVE5 = "NGA_2023_GHSP-W5_v02_M_CSV.zip"


def survey_mass_factor(unit_codes: pd.Series, size_codes: pd.Series, provided: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Use the provider's factor, including crop-condition adjustments.

    One native kg of an unshelled crop may legitimately have a factor below 1.
    Missing provider factors are not reconstructed from container labels.
    """
    factor = numeric(provided).where(lambda s: s.gt(0))
    volume = (unit_codes.isin([3, 190]) | size_codes.isin([5, 6])).fillna(False)
    ambiguous = unit_codes.eq(900).fillna(False)
    factor = factor.mask(volume | ambiguous)
    status = pd.Series("survey_conversion_factor", index=factor.index, dtype="string")
    status.loc[factor.isna()] = "missing_conversion_factor"
    status.loc[ambiguous] = "unspecified_unit_basis"
    status.loc[volume] = "volume_basis_not_kg"
    return factor, status


def derive_loss(harvest: pd.Series, indicator: pd.Series, quantity: pd.Series) -> pd.DataFrame:
    """Q12 option 8 gates Q20; a missing gated response is not a generic zero."""
    harvest, indicator, quantity = numeric(harvest), numeric(indicator), numeric(quantity)
    conflict = indicator.eq(0) & quantity.notna() & quantity.ne(0)
    loss = quantity.where(indicator.eq(1))
    loss = loss.mask(indicator.eq(0) & ~conflict, 0.0)
    valid = harvest.gt(0) & loss.ge(0) & loss.le(harvest) & ~conflict
    status = pd.Series("unanswered_loss_indicator", index=harvest.index, dtype="string")
    status.loc[indicator.eq(0)] = "confirmed_no_loss"
    status.loc[indicator.eq(1) & quantity.notna()] = "reported_loss_quantity"
    status.loc[indicator.eq(1) & quantity.isna()] = "missing_reported_loss_quantity"
    status.loc[conflict] = "conflicting_loss_response"
    invalid_quantity = loss.notna() & (~harvest.gt(0) | loss.lt(0) | loss.gt(harvest))
    status.loc[invalid_quantity] = "invalid_quantity_or_denominator"
    return pd.DataFrame({
        "lost_native_units": loss.where(valid),
        "loss_rate_pct": safe_ratio(loss.where(valid), harvest) * 100,
        "loss_response_status": status,
        "valid_native_loss_measure": valid,
    })


def build_survey(sources: Sources, config: Config, quality: Quality) -> tuple[pd.DataFrame, pd.DataFrame]:
    raw = sources.csv("secta3ii_harvestw5.csv", archive=WAVE5)
    required = ["hhid", "cropcode", "state", "lga", "zone", "sa3iiq3a", "sa3iiq3b", "sa3iiq3c",
                "sa3iiq3d", "sa3iiq1_conv", "sa3iiq12_8", "sa3iiq20", "sa3iiq4", "sa3iiq6", "sa3iiq7"]
    require_columns(raw, required, "wave5_crop_disposition")
    if raw.empty:
        raise DataQualityError("Crop disposition contains no rows")
    out = pd.DataFrame(index=raw.index)
    out["hhid"] = identifier(raw.hhid)
    out["crop_code"] = codes(raw.cropcode)
    out["crop_label"] = labels(raw.cropcode)
    out["commodity"] = out.crop_code.map(SURVEY_CROPS).astype("string")
    require_unique(out, ["hhid", "crop_code"], "survey_crop_disposition")
    if out.commodity.isna().any():
        raise DataQualityError("Unmapped survey crop codes: extend the explicit crop mapping before processing")
    for field in ("state", "lga", "zone"):
        out[field + "_code"] = codes(raw[field])
        out[field] = raw[field].map(normalise_place).astype("string")
    out["survey_season"] = config.survey_season
    out["price_reference_year"] = config.survey_price_year
    out["in_project_scope"] = out.commodity.isin(config.target_commodities)
    out["harvest_native_units"] = numeric(raw.sa3iiq3a)
    out["harvest_unit_code"] = codes(raw.sa3iiq3b)
    out["harvest_unit_label"] = labels(raw.sa3iiq3b)
    out["harvest_size_code"] = codes(raw.sa3iiq3c)
    out["harvest_size_label"] = labels(raw.sa3iiq3c)
    out["harvest_condition_code"] = codes(raw.sa3iiq3d)
    out["harvest_condition_label"] = labels(raw.sa3iiq3d)
    out["condition_group"] = out.harvest_condition_code.map({1: "unshelled", 2: "shelled"}).fillna("not_recorded")
    out["reported_loss_indicator"] = numeric(raw.sa3iiq12_8).astype("Int64")
    out["reported_loss_native_units_raw"] = numeric(raw.sa3iiq20)
    derived = derive_loss(out.harvest_native_units, out.reported_loss_indicator, out.reported_loss_native_units_raw)
    out = pd.concat([out, derived], axis=1)

    factor, conversion_status = survey_mass_factor(out.harvest_unit_code, out.harvest_size_code, raw.sa3iiq1_conv)
    out["survey_conversion_factor_raw"] = numeric(raw.sa3iiq1_conv)
    out["kg_per_native_unit"] = factor
    out["mass_conversion_status"] = conversion_status
    out["harvest_kg"] = out.harvest_native_units.where(out.harvest_native_units.gt(0)) * factor
    out["lost_kg"] = out.lost_native_units * factor
    out["lost_tonnes"] = out.lost_kg / 1000
    out["valid_kg_loss_measure"] = out.valid_native_loss_measure & out.harvest_kg.notna() & out.lost_kg.notna()

    sold = numeric(raw.sa3iiq6)
    sales = numeric(raw.sa3iiq7)
    valid_sale = codes(raw.sa3iiq4).eq(1).fillna(False) & sold.gt(0) & sold.le(out.harvest_native_units) & sales.gt(0)
    out["sold_native_units"] = sold
    out["sales_value_ngn"] = sales
    out["own_unit_value_ngn_per_native_unit"] = safe_ratio(sales.where(valid_sale), sold)
    out["own_unit_value_ngn_per_kg"] = safe_ratio(sales.where(valid_sale), sold * factor)
    out["own_unit_value_loss_ngn"] = out.lost_native_units * out.own_unit_value_ngn_per_native_unit
    out.loc[out.lost_native_units.eq(0), "own_unit_value_loss_ngn"] = 0.0
    out["sale_location_code"] = codes(raw.sa3iiq8) if "sa3iiq8" in raw else pd.NA
    out["sale_year"] = numeric(raw.sa3iiq5b).astype("Int64") if "sa3iiq5b" in raw else pd.NA
    out["sale_month"] = codes(raw.sa3iiq5a) if "sa3iiq5a" in raw else pd.NA
    out["market_product_group"] = pd.Series(
        [survey_market_group(c, k) for c, k in zip(out.commodity, out.harvest_condition_code)],
        index=out.index, dtype="string",
    )

    household = sources.csv("secta_harvestw5.csv", archive=WAVE5)
    require_columns(household, ["hhid", "wt_cross_wave5", "cluster", "strata", "interviewstart"], "household_cover")
    cover = pd.DataFrame({
        "hhid": identifier(household.hhid),
        "survey_weight": numeric(household.wt_cross_wave5),
        "survey_cluster": identifier(household.cluster),
        "survey_stratum": household.strata,
        "interview_date": pd.to_datetime(household.interviewstart, errors="coerce"),
    })
    out = left_join(out, cover, ["hhid"], "survey_cross_sectional_weights", quality, minimum_match=1)
    if out.survey_weight.isna().any() or out.survey_weight.le(0).any():
        raise DataQualityError("Every retained survey row needs a positive wt_cross_wave5")

    plot = sources.csv("secta3i_harvestw5.csv", archive=WAVE5)
    require_columns(plot, ["hhid", "plotid", "cropcode", "sa3iq5b"], "field_crop_harvest")
    p = pd.DataFrame({
        "hhid": identifier(plot.hhid), "plotid": identifier(plot.plotid), "crop_code": codes(plot.cropcode),
        "field_harvest_start_year": numeric(plot.sa3iq5b),
    })
    require_unique(p, ["hhid", "plotid", "crop_code"], "field_crop_harvest")
    timing = p.groupby(["hhid", "crop_code"], dropna=False).agg(
        field_plot_records=("plotid", "size"),
        field_harvest_start_year_min=("field_harvest_start_year", "min"),
        field_harvest_start_year_max=("field_harvest_start_year", "max"),
    ).reset_index()
    out = left_join(out, timing, ["hhid", "crop_code"], "optional_field_crop_timing", quality)

    geo = sources.csv("nga_householdgeovariables_y5.csv", archive=WAVE5)
    geo_fields = [c for c in ["dist_mainroad", "dist_allroad", "dist_city", "dist_admcap", "ttnatc_hr",
                              "y2023_anntot", "avg_anntot", "lat_mod", "lon_mod"] if c in geo]
    geotable = pd.DataFrame({"hhid": identifier(geo.hhid)})
    for field in geo_fields:
        geotable["geo_" + field] = numeric(geo[field])
    out = left_join(out, geotable, ["hhid"], "household_geovariables", quality)

    crosswalk = out[["crop_code", "crop_label", "commodity", "in_project_scope"]].drop_duplicates().sort_values("crop_code")
    for name, value in {
        "survey_rows": len(out), "survey_households": out.hhid.nunique(),
        "survey_focus_rows": out.in_project_scope.sum(),
        "survey_reported_loss_rows": out.reported_loss_indicator.eq(1).sum(),
        "survey_valid_native_loss_rows": out.valid_native_loss_measure.sum(),
        "survey_valid_kg_loss_rows": out.valid_kg_loss_measure.sum(),
        "survey_conversion_unavailable_rows": out.kg_per_native_unit.isna().sum(),
    }.items():
        quality.metric(name, value)
    quality.issue("survey_snapshot", "Wave 5 is a 2023/24 survey season, not a 2014-2026 annual household panel.")
    quality.issue("survey_loss_definition", "Reported loss includes insects, rodents and theft as well as rotting. It is not entirely preventable by cold storage.")
    quality.issue("survey_weights", "Point estimates use wt_cross_wave5. This pipeline does not estimate design-based standard errors or confidence intervals.")
    quality.issue("survey_conversion", "Volume-like or unspecified unit bases and missing factors are not treated as kilograms.", rows=int(out.kg_per_native_unit.isna().sum()))
    quality.issue("price_reference_period", "External survey price proxies use a configured calendar year, not an assumed exact transaction date.", year=config.survey_price_year)
    return out, crosswalk


# ========================================================================
# Market prices and valuation
# ========================================================================

def calendar_features(prices: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Calendar-month, past-only baselines; missing prices stay missing."""
    keys = ["source", "state", "commodity", "product_group", "price_type"]
    parts = []
    for key, block in prices.groupby(keys, dropna=False, sort=True):
        block = block.sort_values("period").set_index("period")
        if block.index.has_duplicates:
            raise ValueError("State price series has duplicate months")
        index = pd.date_range(block.index.min(), block.index.max(), freq="MS")
        current = block.price_ngn_per_kg.reindex(index)
        history = current.shift(1)
        rolling = history.rolling(config.rolling_months, min_periods=config.minimum_rolling_months)
        mean, std = rolling.mean(), rolling.std(ddof=1)
        count = history.notna().rolling(config.rolling_months, min_periods=1).sum().astype(int)
        frame = pd.DataFrame({
            "period": index, "price_ngn_per_kg": current.to_numpy(),
            "price_observed": current.notna().to_numpy(),
            "baseline_observed_months": count.to_numpy(),
            "past_window_mean_ngn_per_kg": mean.to_numpy(),
            "past_window_std_ngn_per_kg": std.to_numpy(),
            "price_volatility_cv_pct": safe_ratio(std, mean).to_numpy() * 100,
            "price_shock_ratio": safe_ratio(current, mean).to_numpy(),
        })
        spike = frame.price_shock_ratio.gt(config.spike_ratio).astype("boolean")
        frame["is_price_spike"] = spike.where(frame.price_shock_ratio.notna())
        frame["baseline_window_months"] = config.rolling_months
        for column, value in zip(keys, key):
            frame[column] = value
        frame["year"] = frame.period.dt.year
        frame["month"] = frame.period.dt.month
        parts.append(frame)
    if parts:
        return pd.concat(parts, ignore_index=True)
    return pd.DataFrame(columns=keys + ["period", "price_ngn_per_kg", "year", "month"])


def build_markets(sources: Sources, config: Config, quality: Quality) -> dict[str, pd.DataFrame]:
    fact = sources.csv("wfp_food_prices_nga.csv")
    dim = sources.csv("wfp_markets_nga.csv")
    require_columns(fact, ["date", "admin1", "admin2", "market", "market_id", "commodity", "unit",
                           "priceflag", "pricetype", "currency", "price", "latitude", "longitude"], "wfp")
    require_columns(dim, ["market_id", "latitude", "longitude"], "wfp_market_dimension")
    fact["market_id"] = identifier(fact.market_id)
    dim["market_id"] = identifier(dim.market_id)
    dim = dim[["market_id", "latitude", "longitude"]].rename(
        columns={"latitude": "dimension_latitude", "longitude": "dimension_longitude"})
    fact = left_join(fact, dim, ["market_id"], "wfp_market_coordinates", quality, minimum_match=1)

    fews = sources.csv("FEWS_NET_Staple_Food_Price_Data.csv")
    require_columns(fews, ["period_date", "admin_1", "market", "product", "unit", "currency", "value", "price_type"], "fews")
    tables = []
    exclusions = []
    product_maps = []
    coverage_parts = []
    for source, raw in (("wfp", fact), ("fewsnet", fews)):
        wfp = source == "wfp"
        frame = pd.DataFrame(index=raw.index)
        dates = pd.to_datetime(raw["date" if wfp else "period_date"], format="%Y-%m-%d", errors="coerce")
        frame["date"] = dates
        frame["source"] = source
        frame["state"] = raw["admin1" if wfp else "admin_1"].map(normalise_place).astype("string")
        frame["lga"] = raw.admin2.map(normalise_place).astype("string") if wfp else pd.Series(pd.NA, index=raw.index, dtype="string")
        frame["market_name"] = raw.market
        frame["market_id"] = raw.market_id if wfp else (frame.state + ":" + raw.market.map(normalise_place))
        frame["product_label"] = raw["commodity" if wfp else "product"].str.strip()
        product = frame.product_label.str.lower().map(MARKET_PRODUCTS)
        frame["commodity"] = product.map(lambda value: value[0] if isinstance(value, tuple) else pd.NA).astype("string")
        frame["product_group"] = product.map(lambda value: value[1] if isinstance(value, tuple) else pd.NA).astype("string")
        frame["price_type"] = raw["pricetype" if wfp else "price_type"].str.strip().str.title()
        frame["currency"] = raw.currency.str.strip().str.upper()
        frame["unit_raw"] = raw.unit
        frame["kg_per_price_unit"] = raw.unit.map(mass_kg).astype(float)
        frame["price_native"] = numeric(raw["price" if wfp else "value"])
        frame["price_ngn_per_kg"] = safe_ratio(frame.price_native, frame.kg_per_price_unit)
        frame["latitude"] = numeric(raw.latitude)
        frame["longitude"] = numeric(raw.longitude)
        frame["price_flag"] = raw.priceflag if wfp else "source_observation"
        if wfp:
            frame["latitude"] = frame.latitude.fillna(numeric(raw.dimension_latitude))
            frame["longitude"] = frame.longitude.fillna(numeric(raw.dimension_longitude))
            conflict = (numeric(raw.latitude).sub(numeric(raw.dimension_latitude)).abs().gt(0.001)
                        | numeric(raw.longitude).sub(numeric(raw.dimension_longitude)).abs().gt(0.001))
            quality.metric("wfp_coordinate_conflict_rows", int(conflict.sum()))
        period_ok = dates.dt.year.between(config.start_year, config.end_year)
        flags_ok = raw.priceflag.isin(config.wfp_price_flags) if wfp else pd.Series(True, index=raw.index)
        checks = {
            "outside_requested_years_or_invalid_date": ~period_ok,
            "unmapped_product": frame.commodity.isna(),
            "not_explicit_mass_unit": frame.kg_per_price_unit.isna(),
            "missing_or_nonpositive_price": ~frame.price_native.gt(0),
            "not_ngn": frame.currency.ne("NGN"),
            "excluded_price_flag": ~flags_ok,
            "missing_state_or_market": frame.state.isna() | frame.market_id.isna(),
        }
        eligible = pd.Series(True, index=raw.index)
        for reason, invalid in checks.items():
            invalid = invalid.fillna(True)
            eligible &= ~invalid
            exclusions.append({"source": source, "reason": reason, "rows": int(invalid.sum()), "counts_can_overlap": True})
        clean = frame.loc[eligible].copy()
        clean["period"] = clean.date.dt.to_period("M").dt.to_timestamp()
        tables.append(clean)
        coverage = frame.loc[frame.date.notna(), ["source", "date", "price_type", "price_flag"]].copy()
        coverage["year"] = coverage.date.dt.year
        coverage["eligible"] = eligible.loc[coverage.index].astype(int)
        coverage_parts.append(coverage.groupby(["source", "year", "price_type", "price_flag"], dropna=False).agg(
            raw_observations=("date", "size"), eligible_mass_price_observations=("eligible", "sum"),
            earliest_date=("date", "min"), latest_date=("date", "max"),
        ).reset_index())
        product_maps.append(frame[["source", "product_label", "commodity", "product_group"]].drop_duplicates())
        quality.metric(source + "_raw_rows", len(raw))
        quality.metric(source + "_usable_mass_price_rows", len(clean))
        quality.metric(source + "_observed_date_min", dates.min().date().isoformat() if dates.notna().any() else None)
        quality.metric(source + "_observed_date_max", dates.max().date().isoformat() if dates.notna().any() else None)
        if wfp and not (clean.date.dt.year.eq(config.survey_price_year) & clean.price_type.eq("Wholesale")).any():
            quality.issue("wfp_wholesale_price_year", "No WFP wholesale observations are eligible in the configured survey price year under the source-flag and unit rules. The wholesale lookup remains unmatched; it is not filled with retail prices.", year=config.survey_price_year)

    observations = pd.concat(tables, ignore_index=True)
    keys = ["source", "state", "lga", "market_id", "market_name", "commodity", "product_group", "product_label", "price_type", "period"]
    monthly = observations.groupby(keys, dropna=False).agg(
        price_ngn_per_kg=("price_ngn_per_kg", "mean"),
        source_observations=("price_native", "size"), source_dates=("date", "nunique"),
        original_unit_count=("unit_raw", "nunique"),
        latitude=("latitude", "first"), longitude=("longitude", "first"),
    ).reset_index()
    # Average variants within a market first, then markets within a location.
    market_keys = ["source", "state", "lga", "market_id", "commodity", "product_group", "price_type", "period"]
    market_groups = monthly.groupby(market_keys, dropna=False).agg(
        price_ngn_per_kg=("price_ngn_per_kg", "mean"), product_variants=("product_label", "nunique")
    ).reset_index()
    regional_parts = []
    for level in ("state", "lga"):
        block = market_groups if level == "state" else market_groups.loc[market_groups.source.eq("wfp") & market_groups.lga.notna()]
        region_keys = ["source", "state"] + (["lga"] if level == "lga" else []) + ["commodity", "product_group", "price_type", "period"]
        regional = block.groupby(region_keys, dropna=False).agg(
            price_ngn_per_kg=("price_ngn_per_kg", "mean"),
            markets_observed=("market_id", "nunique"),
            maximum_variants_per_market=("product_variants", "max"),
        ).reset_index()
        regional["geography_level"] = level
        if "lga" not in regional:
            regional["lga"] = pd.Series(pd.NA, index=regional.index, dtype="string")
        regional["lga"] = regional.lga.astype("string")
        regional_parts.append(regional)
    regional = pd.concat(regional_parts, ignore_index=True)
    regional["year"] = regional.period.dt.year
    regional["month"] = regional.period.dt.month
    annual_keys = ["source", "geography_level", "state", "lga", "commodity", "product_group", "price_type", "year"]
    annual = regional.groupby(annual_keys, dropna=False).agg(
        mean_observed_month_price_ngn_per_kg=("price_ngn_per_kg", "mean"),
        months_observed=("period", "nunique"),
        minimum_monthly_price_ngn_per_kg=("price_ngn_per_kg", "min"),
        maximum_monthly_price_ngn_per_kg=("price_ngn_per_kg", "max"),
        maximum_markets_observed=("markets_observed", "max"),
    ).reset_index()
    annual["eligible_annual_proxy"] = annual.months_observed.ge(config.minimum_price_months)
    annual["price_ngn_per_kg"] = annual.mean_observed_month_price_ngn_per_kg.where(annual.eligible_annual_proxy)
    features = calendar_features(regional.loc[regional.geography_level.eq("state")].copy(), config)
    quality.issue("market_mass_units", "Prices per tuber, piece or litre are not converted to NGN/kg. Gari is not priced as fresh cassava, and milled rice is not priced as paddy.")
    quality.issue("market_proxy", "Regional proxies average product variants within markets and then markets within months. They are calendar-period market proxies, not observed farmgate prices.")
    quality.issue("market_geography", "Only exact normalized place names and documented FCT aliases are matched. No fuzzy matches or national price fallback are used.")
    quality.issue("price_flags", "WFP aggregate-only rows are excluded by default to avoid treating aggregates as additional markets.", included=list(config.wfp_price_flags))
    quality.metric("market_monthly_rows", len(monthly))
    quality.metric("regional_annual_rows", len(annual))
    return {
        "market_prices_monthly": monthly,
        "market_prices_regional_monthly": regional,
        "market_prices_annual": annual,
        "market_price_features": features,
        "market_exclusions": pd.DataFrame(exclusions),
        "market_product_crosswalk": pd.concat(product_maps, ignore_index=True),
        "market_source_coverage": pd.concat(coverage_parts, ignore_index=True),
    }


def attach_prices(survey: pd.DataFrame, annual: pd.DataFrame, config: Config, quality: Quality) -> pd.DataFrame:
    out = survey.copy()
    choices = []
    price_types = ["Wholesale"] + (["Retail"] if config.allow_retail_valuation else [])
    for price_type in price_types:
        for source, level in [("wfp", "lga"), ("wfp", "state"), ("fewsnet", "state")]:
            tag = f"{source}_{level}_{price_type.lower()}"
            block = annual.loc[
                annual.source.eq(source) & annual.geography_level.eq(level)
                & annual.price_type.eq(price_type) & annual.eligible_annual_proxy
            ].copy()
            keys = ["state", "market_product_group", "price_reference_year"]
            original_keys = ["state", "product_group", "year"]
            if level == "lga":
                keys.append("lga")
                original_keys.append("lga")
            block = block[original_keys + ["price_ngn_per_kg", "months_observed"]].rename(columns={
                "product_group": "market_product_group", "year": "price_reference_year",
                "price_ngn_per_kg": tag + "_ngn_per_kg", "months_observed": tag + "_months",
            })
            out = left_join(out, block, keys, "survey_price_" + tag, quality)
            choices.append(tag)

    proxy = pd.Series(np.nan, index=out.index)
    method = pd.Series(pd.NA, index=out.index, dtype="string")
    for tag in choices:
        selected = proxy.isna() & out[tag + "_ngn_per_kg"].notna()
        proxy.loc[selected] = out.loc[selected, tag + "_ngn_per_kg"]
        method.loc[selected] = tag + "_calendar_proxy"
    out["external_proxy_price_ngn_per_kg"] = proxy
    out["external_proxy_method"] = method
    out["external_proxy_loss_value_ngn"] = out.lost_kg * proxy
    out["estimated_loss_value_ngn"] = out.own_unit_value_loss_ngn
    out["valuation_method"] = pd.Series("not_valued", index=out.index, dtype="string")
    own = out.own_unit_value_loss_ngn.notna() & out.lost_native_units.gt(0)
    out.loc[own, "valuation_method"] = "own_sales_unit_value"
    external = out.estimated_loss_value_ngn.isna() & out.external_proxy_loss_value_ngn.notna()
    out.loc[external, "estimated_loss_value_ngn"] = out.loc[external, "external_proxy_loss_value_ngn"]
    out.loc[external, "valuation_method"] = method.loc[external]
    zero = out.lost_native_units.eq(0)
    out.loc[zero, "estimated_loss_value_ngn"] = 0.0
    out.loc[zero, "valuation_method"] = "confirmed_zero_loss"
    out["positive_loss_unvalued"] = out.lost_native_units.gt(0) & out.estimated_loss_value_ngn.isna()
    quality.metric("survey_positive_loss_valued_rows", int((out.lost_native_units.gt(0) & out.estimated_loss_value_ngn.notna()).sum()))
    quality.metric("survey_positive_loss_unvalued_rows", int(out.positive_loss_unvalued.sum()))
    quality.issue("valuation", "Own reported sales unit values take priority. Wholesale regional prices are optional proxies for compatible forms. Retail is disabled by default. Unvalued positive losses remain missing.")
    return out


# ========================================================================
# Production and external loss benchmarks
# ========================================================================

def build_benchmarks(sources: Sources, config: Config, quality: Quality) -> dict[str, pd.DataFrame]:
    fao = sources.csv("FAOSTAT_data_en_9-19-2026.csv")
    require_columns(fao, ["area", "item", "year", "element", "unit", "value"], "faostat")
    fao["year"] = numeric(fao.year)
    fao["commodity"] = fao.item.str.strip().str.lower().map(FAO_PRODUCTS).astype("string")
    fao["value"] = numeric(fao.value)
    scope = (fao.area.str.strip().str.lower().eq("nigeria")
             & fao.year.between(config.start_year, config.end_year)
             & fao.commodity.isin(config.target_commodities))
    selected = fao.loc[scope].copy()
    production = selected.loc[selected.element.eq("Production") & selected.unit.eq("t")].copy()
    if production.empty:
        raise DataQualityError("No national crop production records match the configured scope")
    if production.value.isna().any() or production.value.lt(0).any():
        raise DataQualityError("Invalid national production values")
    production = production[["year", "commodity", "item", "value"]].rename(columns={
        "item": "faostat_item", "value": "national_production_tonnes"})
    production["year"] = production.year.astype(int)
    require_unique(production, ["year", "commodity"], "faostat_production")
    yields = selected.loc[selected.element.eq("Yield") & selected.unit.eq("kg/ha"), ["year", "commodity", "item", "value"]].rename(
        columns={"item": "faostat_item", "value": "yield_kg_per_hectare"})
    yields["year"] = yields.year.astype(int)
    require_unique(yields, ["year", "commodity"], "faostat_yield")
    exclusions = fao.loc[~scope].groupby(["item", "element", "unit"], dropna=False).size().reset_index(name="rows_excluded")

    loss = sources.csv("Data.csv")
    require_columns(loss, ["country", "commodity", "year", "loss_percentage", "food_supply_stage",
                           "activity", "method_data_collection", "reference", "url"], "fao_food_loss")
    loss["year"] = numeric(loss.year)
    loss = loss.loc[loss.country.str.strip().str.lower().eq("nigeria") & loss.year.between(config.start_year, config.end_year)].copy()
    loss["commodity_original"] = loss.commodity
    loss["commodity"] = loss.commodity_original.str.strip().str.lower().map(FAO_PRODUCTS).astype("string")
    loss["loss_percentage"] = numeric(loss.loss_percentage)
    if loss.loss_percentage.isna().any() or (~loss.loss_percentage.between(0, 100)).any():
        raise DataQualityError("Food-loss percentages must be numeric and between 0 and 100")
    loss["year"] = loss.year.astype(int)
    loss["source_family"] = "other_fao_flw_entry"
    aphlis = loss.reference.str.contains("aphlis", case=False, na=False) | loss.url.str.contains("aphlis.net", case=False, na=False, regex=False)
    loss.loc[aphlis, "source_family"] = "aphlis"
    loss["in_project_scope"] = loss.commodity.isin(config.target_commodities)
    keys = ["year", "commodity", "commodity_original", "food_supply_stage", "activity", "method_data_collection", "reference", "source_family"]
    summary = loss.groupby(keys, dropna=False).agg(
        observation_count=("loss_percentage", "size"),
        descriptive_mean_loss_pct=("loss_percentage", "mean"),
        median_loss_pct=("loss_percentage", "median"),
        minimum_loss_pct=("loss_percentage", "min"), maximum_loss_pct=("loss_percentage", "max"),
    ).reset_index()
    quality.metric("faostat_production_rows", len(production))
    quality.metric("faostat_production_year_min", int(production.year.min()))
    quality.metric("faostat_production_year_max", int(production.year.max()))
    quality.metric("loss_benchmark_rows", len(loss))
    quality.metric("loss_benchmark_latest_year", int(loss.year.max()) if len(loss) else None)
    quality.metric("loss_benchmark_aphlis_rows", int(aphlis.sum()))
    quality.issue("benchmark_coverage", "External loss observations are kept with their actual study years, stages, activities, methods and references. No carry-forward to 2023/24 or national loss estimate is fabricated.")
    quality.issue("national_context", "National production is contextual and non-additive when repeated on household rows. Do not sum it in a survey dashboard.")
    return {
        "national_production_annual": production.sort_values(["commodity", "year"]),
        "national_yield_annual": yields.sort_values(["commodity", "year"]),
        "faostat_excluded_items": exclusions,
        "loss_benchmarks": loss.reset_index(drop=True),
        "loss_benchmarks_summary": summary,
    }


# ========================================================================
# Climate point processing
# ========================================================================

NASA_FILE = "POWER_Point_Monthly_20140101_20241231.csv"
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def build_climate(sources: Sources, config: Config, quality: Quality) -> dict[str, pd.DataFrame]:
    name = sources.find(NASA_FILE)
    content = sources.bytes(name)
    text = content.decode("utf-8-sig")
    lines = text.splitlines()
    end = next((i for i, line in enumerate(lines) if "-END HEADER-" in line), None)
    if end is None:
        raise DataQualityError("NASA POWER file is missing its metadata terminator")
    header = "\n".join(lines[:end])
    coordinates = re.search(r"Latitude\s+(-?[\d.]+)\s+Longitude\s+(-?[\d.]+)", header)
    if not coordinates:
        raise DataQualityError("NASA POWER location is absent from metadata")
    lat, lon = map(float, coordinates.groups())
    units = {}
    for line in lines[:end]:
        match = re.match(r"^([A-Z][A-Z0-9_]+)\s+.+\(([^()]+)\)\s*$", line.strip())
        if match:
            units[match.group(1)] = match.group(2)
    raw = pd.read_csv(StringIO("\n".join(lines[end + 1:])), dtype="string")
    raw.columns = raw.columns.str.strip().str.lower()
    sources.record(name, content, raw)
    require_columns(raw, ["parameter", "year", "ann"] + MONTHS, "nasa_power")
    raw["year"] = numeric(raw.year)
    raw = raw.loc[raw.year.between(config.start_year, config.end_year)].copy()
    raw["year"] = raw.year.astype(int)
    for column in MONTHS + ["ann"]:
        raw[column] = numeric(raw[column]).replace(-999, np.nan)
    require_unique(raw, ["year", "parameter"], "nasa_parameters")
    long = raw.melt(id_vars=["parameter", "year"], value_vars=MONTHS, var_name="month_label", value_name="value_native")
    long["month"] = long.month_label.map({m: i + 1 for i, m in enumerate(MONTHS)})
    long["unit_declared"] = long.parameter.map(units)
    long["unit_status"] = "as_declared_in_source"
    uncertain = long.parameter.eq("PRECTOTCORR_SUM") & long.unit_declared.eq("mm/day")
    long.loc[uncertain, "unit_status"] = "sum_parameter_rate_unit_conflict_verify_before_use"
    long["latitude"] = lat
    long["longitude"] = lon
    long["point_id"] = f"nasa_{lat}_{lon}"
    long["period"] = pd.to_datetime(dict(year=long.year, month=long.month, day=1))
    wide = long.pivot(index=["point_id", "year", "month", "latitude", "longitude", "period"], columns="parameter", values="value_native").reset_index()
    wide.columns.name = None
    annual = raw[["parameter", "year", "ann"]].rename(columns={"ann": "source_annual_value_native"})
    annual["unit_declared"] = annual.parameter.map(units)
    annual["latitude"] = lat
    annual["longitude"] = lon
    annual["point_id"] = f"nasa_{lat}_{lon}"
    annual["aggregation"] = "ANN_value_provided_by_source"
    quality.metric("nasa_monthly_rows", len(wide))
    quality.metric("nasa_points", 1)
    quality.metric("nasa_year_max", int(raw.year.max()) if len(raw) else None)
    quality.issue("climate_spatial_scope", "NASA POWER is one point and is exported separately. It is not assigned to every household or used to create a nationwide spoilage-risk score.", latitude=lat, longitude=lon)
    if uncertain.any():
        quality.issue("precipitation_unit_conflict", "The supplied PRECTOTCORR_SUM header says mm/day although the parameter is named a sum. Native values and source ANN are preserved; no unit correction or derived rainfall-risk calculation is assumed.")
    return {"climate_point_monthly": wide, "climate_point_monthly_long": long, "climate_point_annual": annual}


# ========================================================================
# Integration and exports
# ========================================================================

GRAINS = {
    "survey_crop_loss": "household, crop code, survey season; all survey crops",
    "analysis_survey": "household, crop code, survey season; configured focus crops",
    "survey_crop_crosswalk": "survey crop code and label",
    "market_prices_monthly": "source, market, product label, price type, calendar month",
    "market_prices_regional_monthly": "source, geography level, state/LGA, product group, price type, month",
    "market_prices_annual": "source, geography level, state/LGA, product group, price type, calendar year",
    "market_price_features": "source, state, product group, price type, calendar month including observed gaps",
    "market_exclusions": "source and exclusion reason; counts overlap",
    "market_product_crosswalk": "source and original product label",
    "market_source_coverage": "source, year, price type, source flag; raw and eligible observation counts",
    "national_production_annual": "Nigeria, canonical commodity, calendar year",
    "national_yield_annual": "Nigeria, canonical commodity, calendar year",
    "faostat_excluded_items": "original item, element, unit",
    "loss_benchmarks": "original FAO FLW observation with source metadata",
    "loss_benchmarks_summary": "study year, original commodity form, stage, activity, method, reference",
    "climate_point_monthly": "NASA point, year, month",
    "climate_point_monthly_long": "NASA point, parameter, year, month",
    "climate_point_annual": "NASA point, parameter, year; source ANN value",
    "survey_summary_crop": "commodity and harvest condition",
    "survey_summary_zone_crop": "zone, commodity and harvest condition",
    "survey_summary_state_crop": "state, commodity and harvest condition; descriptive",
}

FIELD_NOTES = {
    "loss_rate_pct": "100 * loss native units / harvest native units; 0-100 percentage points",
    "reported_loss_indicator": "Q12 option 8: 1 reported loss, 0 explicitly not selected",
    "kg_per_native_unit": "provider conversion factor, including crop-condition adjustments; missing factors and volume bases stay unavailable",
    "survey_weight": "wt_cross_wave5; cross-sectional household weight",
    "estimated_loss_value_ngn": "nominal NGN; own raw-sales unit value first, otherwise compatible wholesale proxy; see valuation_method",
    "own_unit_value_loss_ngn": "native-unit loss multiplied by own unit value, allowing valuation without a kg conversion",
    "external_proxy_loss_value_ngn": "lost kg times compatible regional annual price; not observed farmgate revenue",
    "national_production_context_tonnes": "non-additive national context repeated on survey rows; never sum over households",
    "price_reference_year": "configured calendar-year proxy for the survey season; not a recorded transaction year",
    "price_ngn_per_kg": "nominal NGN per explicit kg; price units converted before aggregation",
    "price_volatility_cv_pct": "sample std/mean*100 over available preceding calendar months; minimum coverage required",
    "is_price_spike": "nullable: unknown when current price or lagged baseline is unavailable",
    "source_annual_value_native": "ANN value from the NASA file; aggregation and units are parameter-specific",
    "geo_lat_mod": "provider-modified household latitude; not an exact installation coordinate",
    "geo_lon_mod": "provider-modified household longitude; not an exact installation coordinate",
}


def summarise_survey(data: pd.DataFrame, geography: list[str], config: Config) -> pd.DataFrame:
    keys = geography + ["commodity", "condition_group"]
    records = []
    for key, frame in data.groupby(keys, dropna=False, sort=True):
        row = dict(zip(keys, key if isinstance(key, tuple) else (key,)))
        mass = frame.loc[frame.valid_kg_loss_measure]
        native = frame.loc[frame.valid_native_loss_measure]
        denominator = float((mass.harvest_kg * mass.survey_weight).sum())
        numerator = float((mass.lost_kg * mass.survey_weight).sum())
        mean_denominator = float(native.survey_weight.sum())
        row.update({
            "crop_records": len(frame), "households": frame.hhid.nunique(),
            "reported_loss_records": int(frame.reported_loss_indicator.eq(1).sum()),
            "valid_native_loss_records": len(native), "valid_kg_loss_records": len(mass),
            "records_without_kg_conversion": int(frame.kg_per_native_unit.isna().sum()),
            "sample_harvest_tonnes_paired": float(mass.harvest_kg.sum()) / 1000 if len(mass) else np.nan,
            "sample_lost_tonnes_paired": float(mass.lost_kg.sum()) / 1000 if len(mass) else np.nan,
            "volume_weighted_survey_loss_rate_pct": 100 * numerator / denominator if denominator > 0 else np.nan,
            "household_weighted_mean_loss_rate_pct": float((native.loss_rate_pct * native.survey_weight).sum()) / mean_denominator if mean_denominator > 0 else np.nan,
            "sample_loss_value_ngn_covered": frame.estimated_loss_value_ngn.sum(min_count=1),
            "positive_loss_records_valued": int((frame.lost_native_units.gt(0) & frame.estimated_loss_value_ngn.notna()).sum()),
            "positive_loss_records_unvalued": int(frame.positive_loss_unvalued.sum()),
            "own_sales_positive_loss_valuations": int((frame.lost_native_units.gt(0) & frame.valuation_method.eq("own_sales_unit_value")).sum()),
            "external_proxy_positive_loss_valuations": int((frame.lost_native_units.gt(0) & frame.valuation_method.str.endswith("calendar_proxy", na=False)).sum()),
            "sample_flag": "small_sample_descriptive" if frame.hhid.nunique() < config.low_sample_households else "descriptive_no_precision_estimate",
        })
        records.append(row)
    return pd.DataFrame(records)


def write_tables(tables: dict[str, pd.DataFrame], output: Path, quality: Quality) -> None:
    table_dir = output / "tables"
    table_dir.mkdir()
    schemas = []
    for name, frame in tables.items():
        target = table_dir / (name + ".csv")
        frame.to_csv(target, index=False, encoding="utf-8-sig", date_format="%Y-%m-%d")
        quality.outputs.append({"table": name, "file": "tables/" + target.name, "rows": len(frame), "columns": len(frame.columns), "grain": GRAINS.get(name)})
        for column in frame.columns:
            schemas.append({"table": name, "column": column, "dtype": str(frame[column].dtype),
                            "missing_rows": int(frame[column].isna().sum()), "definition": FIELD_NOTES.get(column, "")})
    pd.DataFrame(schemas).to_csv(output / "data_dictionary.csv", index=False, encoding="utf-8-sig")


def write_report(output: Path, quality: Quality, config: Config) -> None:
    m = quality.metrics
    lines = [
        "# Agricultural pipeline run report", "",
        "The run completed using the supplied source files. Figures below describe the processed sample and coverage, not a national investment forecast.", "",
        "| Check | Result |", "| --- | --- |",
        f"| Requested years | {config.start_year}-{config.end_year} |",
        f"| Survey season | {config.survey_season} |",
        f"| Survey household-crop records | {m['survey_rows']:,} |",
        f"| Survey households | {m['survey_households']:,} |",
        f"| Focus-crop records | {m['survey_focus_rows']:,} |",
        f"| Records reporting a loss, all crops | {m['survey_reported_loss_rows']:,} |",
        f"| Records with a valid kg loss measure | {m['survey_valid_kg_loss_rows']:,} |",
        f"| Positive-loss records valued, all crops | {m['survey_positive_loss_valued_rows']:,} |",
        f"| Positive-loss records unvalued, all crops | {m['survey_positive_loss_unvalued_rows']:,} |",
        f"| National production coverage | {m['faostat_production_year_min']}-{m['faostat_production_year_max']} |",
        f"| Latest loss-benchmark study year | {m['loss_benchmark_latest_year']} |",
        f"| NASA locations | {m['nasa_points']} |", "",
        "Survey loss quantities were converted to zero only when the questionnaire's loss indicator explicitly rules out a loss. Invalid or unanswered responses remain unavailable. Mass conversions preserve the reported crop condition.", "",
        "Own sales unit values provide the first valuation method. External prices are labelled proxies, with geography and months retained. Sample monetary totals cover valued records only. No financial ROI, storage effectiveness or full national monetary total is assumed.", "",
        "Crop summaries retain harvest condition. Point estimates use cross-sectional weights; no design-based confidence intervals are calculated. State results and small groups should be interpreted descriptively.", "",
        "## Join diagnostics", "",
        "| Join | Input rows | Output rows | Matched |", "| --- | ---: | ---: | ---: |",
    ]
    for item in quality.joins:
        rate = f"{item['key_match_rate']:.1%}" if item["key_match_rate"] is not None else "n.a."
        lines.append(f"| {item['name']} | {item['left_rows']:,} | {item['output_rows']:,} | {rate} |")
    lines += ["", "## Coverage and interpretation", ""]
    for issue in quality.issues:
        lines.append(f"- **{issue['code']}**: {issue['message']}")
    lines += ["", "## Outputs", "", "| Table | Rows | What one row represents |", "| --- | ---: | --- |"]
    for table in quality.outputs:
        lines.append(f"| {table['table']} | {table['rows']:,} | {table['grain']} |")
    (output / "RUN_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(input_path: Path, output: Path, config: Config) -> Quality:
    start = time.perf_counter()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Output directory is not empty. Choose a new --output directory.")
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / "run_status.json"
    status = {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat(), "pipeline_version": __version__,
              "python_version": platform.python_version(), "pandas_version": pd.__version__, "config": config.as_dict()}
    status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
    quality = Quality()
    sources = None
    try:
        sources = Sources(input_path, quality)
        logging.info("[1/6] Processing Wave 5 crop disposition, weights, and geography")
        survey, crop_crosswalk = build_survey(sources, config, quality)
        logging.info("[2/6] Processing national production and loss benchmarks")
        tables = build_benchmarks(sources, config, quality)
        logging.info("[3/6] Processing compatible WFP and FEWS NET price series")
        market_tables = build_markets(sources, config, quality)
        tables.update(market_tables)
        logging.info("[4/6] Processing the NASA point as a separate climate output")
        tables.update(build_climate(sources, config, quality))
        logging.info("[5/6] Enriching survey records and calculating covered loss values")
        survey = attach_prices(survey, tables["market_prices_annual"], config, quality)
        context = tables["national_production_annual"].loc[
            tables["national_production_annual"].year.eq(config.survey_price_year),
            ["commodity", "national_production_tonnes"],
        ].rename(columns={"national_production_tonnes": "national_production_context_tonnes"})
        survey = left_join(survey, context, ["commodity"], "national_production_context", quality)
        require_unique(survey, ["hhid", "crop_code", "survey_season"], "final_survey")
        if len(survey) != quality.metrics["survey_rows"]:
            raise DataQualityError("Final survey does not preserve source row count")
        analysis = survey.loc[survey.in_project_scope].copy()
        if analysis.empty:
            raise DataQualityError("No survey records match the configured focus crops")
        tables["survey_crop_loss"] = survey
        tables["analysis_survey"] = analysis
        tables["survey_crop_crosswalk"] = crop_crosswalk
        tables["survey_summary_crop"] = summarise_survey(analysis, [], config)
        tables["survey_summary_zone_crop"] = summarise_survey(analysis, ["zone"], config)
        tables["survey_summary_state_crop"] = summarise_survey(analysis, ["state"], config)
        logging.info("[6/6] Auditing source inventory and exporting tables and diagnostics")
        nupd = sources.archive_inventory("NGA_2010-2019_NUPD_v01_M_CSV.zip")
        (output / "nupd_inventory.json").write_text(json.dumps(nupd, indent=2), encoding="utf-8")
        quality.metric("nupd_members_inventoried", len(nupd))
        quality.issue("nupd_scope", "The supplied NUPD archive is inventoried, not joined to Wave 5. It does not provide a confirmed crop-disposition panel in the files supplied; no annual household crop panel is fabricated.")
        quality.issue("roi_scope", "Infrastructure costs, utilisation and intervention effectiveness were not supplied. The pipeline estimates covered crop-loss value, not infrastructure ROI.")
        write_tables(tables, output, quality)
        quality.metric("runtime_seconds", round(time.perf_counter() - start, 3))
        quality.export(output / "quality_report.json")
        write_report(output, quality, config)
        status.update(status="complete", completed_utc=datetime.now(timezone.utc).isoformat(), runtime_seconds=quality.metrics["runtime_seconds"])
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        logging.info("Complete: %s focus records, %s output tables", len(analysis), len(tables))
        return quality
    except Exception as exc:
        status.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        status_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        raise
    finally:
        if sources:
            sources.close()


# ========================================================================
# Terminal entry point
# ========================================================================

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="AgricProject_Datasets.zip or extracted data directory")
    parser.add_argument("--output", type=Path, help="New/empty output directory")
    parser.add_argument("--config", type=Path, help="Optional JSON configuration")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    output = args.output or Path("results") / datetime.now().strftime("run_%Y%m%d_%H%M%S")
    try:
        config = Config.load(args.config)
        run(args.input.expanduser().resolve(), output.expanduser().resolve(), config)
    except Exception:
        logging.exception("Pipeline failed; see the traceback and run_status.json if created")
        return 1
    print(f"Results: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
