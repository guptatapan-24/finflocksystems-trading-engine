import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


REQUIRED_DATA_FILES = (
    "gics_taxonomy.json",
    "stock_universe.json",
    "strategy_universe.json",
    "compatibility_rules.json",
    "backtest_spec.json",
)
EXPECTED_COUNTS = {"stocks": 500, "strategies": 97, "sectors": 11, "industry_groups": 25, "industries": 74, "sub_industries": 163}
FREQUENCY_MAP = {"1D": "1d", "1H": "1h", "15m": "15m", "5m": "5m", "1m": "1m"}
SCOPE_ROLES = {
    "single_asset": "EXECUTION",
    "cross_sectional": "SELECTOR",
    "portfolio": "ALLOCATOR",
    "overlay": "OVERLAY",
    "pair_or_basket": "PAIR_BASKET",
    "single_asset_or_cross_sectional": "RESEARCH_ONLY",
}


def strategy_role(source):
    """Resolve the operational role from explicit catalogue semantics, not formula text."""
    family = str(source.get("family", "")).lower()
    if family == "event":
        return "EVENT"
    if family == "income":
        return "INCOME"
    return SCOPE_ROLES[source["scope"]]


class BundleValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedBundle:
    root: Path
    manifest: dict
    documents: dict[str, dict]
    manifest_hash: str
    file_hashes: dict[str, str]
    report: dict


def canonical_hash(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"Cannot read valid JSON from {path.name}: {exc}") from exc


def _validate_schema(name, document):
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / f"{name.removesuffix('.json')}.schema.json"
    schema = _load_json(schema_path)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "root"
        raise BundleValidationError(f"{name} schema error at {location}: {first.message}")


def flatten_gics(document):
    rows = []
    for sector in document.get("sectors", []):
        rows.append(("SECTOR", sector, None, [sector["code"]]))
        for group in sector.get("industry_groups", []):
            rows.append(("INDUSTRY_GROUP", group, sector["code"], [sector["code"], group["code"]]))
            for industry in group.get("industries", []):
                rows.append(("INDUSTRY", industry, group["code"], [sector["code"], group["code"], industry["code"]]))
                for sub_industry in industry.get("sub_industries", []):
                    rows.append((
                        "SUB_INDUSTRY", sub_industry, industry["code"],
                        [sector["code"], group["code"], industry["code"], sub_industry["code"]],
                    ))
    return rows


def _semantic_checks(documents, manifest):
    stocks = documents["stock_universe.json"].get("stocks", [])
    strategies = documents["strategy_universe.json"].get("strategies", [])
    rows = flatten_gics(documents["gics_taxonomy.json"])
    level_counts = {level: sum(1 for row in rows if row[0] == level) for level in ("SECTOR", "INDUSTRY_GROUP", "INDUSTRY", "SUB_INDUSTRY")}
    actual = {
        "stocks": len(stocks), "strategies": len(strategies), "sectors": level_counts["SECTOR"],
        "industry_groups": level_counts["INDUSTRY_GROUP"], "industries": level_counts["INDUSTRY"],
        "sub_industries": level_counts["SUB_INDUSTRY"],
    }
    for key, expected in EXPECTED_COUNTS.items():
        if actual[key] != expected:
            raise BundleValidationError(f"Expected {expected} {key}, found {actual[key]}")
    if manifest.get("counts", {}).get("stocks") != len(stocks) or manifest.get("counts", {}).get("strategies") != len(strategies):
        raise BundleValidationError("Manifest counts do not match bundle content")
    codes = [item[1].get("code") for item in rows]
    if len(codes) != len(set(codes)):
        raise BundleValidationError("GICS codes must be unique")
    known_codes = set(codes)
    for level, node, parent, path in rows:
        if not node.get("name") or not node.get("code"):
            raise BundleValidationError("Every GICS node requires a code and name")
        if parent and parent not in known_codes:
            raise BundleValidationError(f"Unknown GICS parent {parent} for {node['code']}")
        expected_lengths = {"SECTOR": 2, "INDUSTRY_GROUP": 4, "INDUSTRY": 6, "SUB_INDUSTRY": 8}
        if len(node["code"]) != expected_lengths[level] or any(not node["code"].startswith(code) for code in path[:-1]):
            raise BundleValidationError(f"Invalid GICS path for {node['code']}")
    sub_industries = {item[1]["code"] for item in rows if item[0] == "SUB_INDUSTRY"}
    symbols = [str(item.get("symbol", "")).upper() for item in stocks]
    ciks = [str(item.get("issuer_metadata", {}).get("cik", "")) for item in stocks]
    if len(symbols) != len(set(symbols)):
        raise BundleValidationError("Stock symbols must be unique")
    if any(not cik for cik in ciks) or len(ciks) != len(set(ciks)):
        raise BundleValidationError("Every stock must have a unique CIK issuer identity")
    for stock in stocks:
        code = str(stock.get("gics", {}).get("sub_industry_code", ""))
        if code not in sub_industries:
            raise BundleValidationError(f"Unknown stock sub-industry {code} for {stock.get('symbol')}")
    excluded = documents["stock_universe.json"].get("metadata", {}).get("excluded_secondary_share_classes", {})
    overlap = sorted(set(symbols).intersection(str(key).upper() for key in excluded))
    if overlap:
        raise BundleValidationError(f"Excluded secondary share classes are present: {', '.join(overlap)}")
    ids = [item.get("id") for item in strategies]
    if len(ids) != len(set(ids)):
        raise BundleValidationError("Strategy IDs must be unique")
    for strategy in strategies:
        scope = strategy.get("scope")
        if scope not in SCOPE_ROLES:
            raise BundleValidationError(f"Unknown strategy scope {scope}")
        for frequency in strategy.get("supported_bar_frequencies", []):
            if frequency not in FREQUENCY_MAP:
                raise BundleValidationError(f"Unknown strategy frequency {frequency} for {strategy.get('id')}")
    return actual


def validate_bundle(bundle_path):
    root = Path(bundle_path).expanduser().resolve()
    if not root.is_dir():
        raise BundleValidationError(f"Research bundle directory does not exist: {root}")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise BundleValidationError("Missing required file manifest.json")
    manifest = _load_json(manifest_path)
    _validate_schema("manifest.json", manifest)
    entries = {item.get("name"): item for item in manifest.get("files", [])}
    if set(entries) != set(REQUIRED_DATA_FILES):
        missing = sorted(set(REQUIRED_DATA_FILES) - set(entries))
        extra = sorted(set(entries) - set(REQUIRED_DATA_FILES))
        raise BundleValidationError(f"Manifest file list mismatch; missing={missing}, extra={extra}")
    documents = {}
    file_hashes = {}
    for name in REQUIRED_DATA_FILES:
        path = root / name
        if not path.is_file():
            raise BundleValidationError(f"Missing required file {name}")
        actual_size = path.stat().st_size
        if actual_size != int(entries[name].get("bytes", -1)):
            raise BundleValidationError(f"Byte count mismatch for {name}")
        actual_hash = _file_hash(path)
        if actual_hash != str(entries[name].get("sha256", "")).lower():
            raise BundleValidationError(f"SHA-256 mismatch for {name}")
        file_hashes[name] = actual_hash
        documents[name] = _load_json(path)
        _validate_schema(name, documents[name])
    actual_counts = _semantic_checks(documents, manifest)
    report = {
        "valid": True,
        "counts": actual_counts,
        "files": {name: {"sha256": file_hashes[name], "bytes": (root / name).stat().st_size} for name in REQUIRED_DATA_FILES},
        "warnings": list(manifest.get("warnings", [])),
        "current_snapshot_only": True,
    }
    return ValidatedBundle(
        root=root,
        manifest=manifest,
        documents=documents,
        manifest_hash=_file_hash(manifest_path),
        file_hashes=file_hashes,
        report=report,
    )
