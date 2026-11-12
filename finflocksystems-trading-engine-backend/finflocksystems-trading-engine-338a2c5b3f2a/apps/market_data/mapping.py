from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone

from apps.instruments.models import InstrumentProviderMapping

from .providers.base import ProviderError, ProviderErrorCode
from .providers.finnhub import FinnhubClient


MIC_BY_IBKR_EXCHANGE = {
    "NASDAQ": {"XNAS"}, "NASDAQ.NMS": {"XNAS"}, "NASDAQCM": {"XNAS"}, "NASDAQGM": {"XNAS"},
    "NYSE": {"XNYS"}, "ARCA": {"ARCX"}, "NYSEARCA": {"ARCX"}, "AMEX": {"XASE"},
    "BATS": {"BATS", "XCBO"}, "IEX": {"IEXG"}, "ASX": {"XASX"}, "LSE": {"XLON"},
    "TSE": {"XTKS"}, "TSX": {"XTSE"}, "SBF": {"XPAR"}, "FWB": {"XFRA"}, "IBIS": {"XETR"},
    "NSE": {"XNSE"}, "XNSE": {"XNSE"}, "BSE": {"XBOM"}, "BOM": {"XBOM"}, "XBOM": {"XBOM"},
}
PROVIDER_EXCHANGE_ALIASES = {
    "NYSE": {"NYSE", "NEWYORKSTOCKEXCHANGE"},
    "NASDAQ": {"NASDAQ"}, "NASDAQ.NMS": {"NASDAQ"}, "NASDAQCM": {"NASDAQ"}, "NASDAQGM": {"NASDAQ"},
    "ARCA": {"NYSEARCA", "ARCA"}, "NYSEARCA": {"NYSEARCA", "ARCA"},
    "AMEX": {"NYSEAMERICAN", "AMEX"}, "BATS": {"BATS", "CBOE"},
    "NSE": {"NSE", "NATIONALSTOCKEXCHANGEOFINDIA"},
    "BSE": {"BSE", "BOMBAYSTOCKEXCHANGE"}, "BOM": {"BSE", "BOMBAYSTOCKEXCHANGE"},
}


def _normalized_symbol(value):
    return str(value or "").strip().upper().replace("/", ".")


def _exchange_matches(instrument, contract, candidate, profile):
    primary = str(contract.primary_exchange or instrument.primary_exchange or "").strip().upper()
    if not primary or primary == "SMART":
        return False, "IBKR primary exchange is unavailable"
    candidate_mic = str(candidate.get("mic") or "").strip().upper()
    allowed_mics = MIC_BY_IBKR_EXCHANGE.get(primary, {primary} if primary.startswith("X") else set())
    if candidate_mic and candidate_mic in allowed_mics:
        return True, "MIC"
    provider_exchange = str(profile.get("provider_exchange") or "").strip().upper()
    normalized_exchange = "".join(character for character in provider_exchange if character.isalnum())
    tokens = {primary, *allowed_mics, *PROVIDER_EXCHANGE_ALIASES.get(primary, set())}
    normalized_tokens = {"".join(character for character in token if character.isalnum()) for token in tokens}
    if normalized_exchange and any(token and token in normalized_exchange for token in normalized_tokens):
        return True, "PROVIDER_EXCHANGE"
    return False, f"Finnhub exchange does not match IBKR primary exchange {primary}"


def _validated_candidate(instrument, contract, candidate, profile):
    provider_symbol = _normalized_symbol(candidate.get("provider_symbol"))
    canonical_symbols = {_normalized_symbol(instrument.symbol), _normalized_symbol(contract.local_symbol)}
    if provider_symbol not in canonical_symbols:
        return None, "provider symbol is not an exact IBKR symbol/local-symbol match"
    profile_symbol = _normalized_symbol(profile.get("provider_symbol"))
    if profile_symbol and profile_symbol != provider_symbol:
        return None, "Finnhub search symbol and company-profile ticker disagree"
    security_type = str(candidate.get("type") or "").strip().lower()
    if security_type and not any(value in security_type for value in ("common stock", "equity", "stock")):
        return None, f"Finnhub security type {candidate.get('type')} is not a supported stock"
    candidate_currency = str(candidate.get("currency") or "").strip().upper()
    profile_currency = str(profile.get("currency") or "").strip().upper()
    if candidate_currency and profile_currency and candidate_currency != profile_currency:
        return None, "Finnhub search and profile currencies disagree"
    currency = candidate_currency or profile_currency
    if not currency or currency != instrument.currency.upper():
        return None, "Finnhub currency is missing or does not match IBKR"
    exchange_matches, evidence = _exchange_matches(instrument, contract, candidate, profile)
    if not exchange_matches:
        return None, evidence
    return {
        "provider_symbol": str(candidate["provider_symbol"]), "currency": currency,
        "exchange_mic": str(candidate.get("mic") or "").upper(),
        "provider_exchange": str(profile.get("provider_exchange") or ""),
        "isin": str(candidate.get("isin") or profile.get("isin") or ""),
        "figi": str(candidate.get("figi") or profile.get("figi") or ""),
        "metadata": {"candidate": candidate.get("raw", candidate), "profile": profile.get("raw", profile),
                     "exchange_evidence": evidence},
    }, ""


def _store_result(mapping, result, *, method):
    mapping.provider_symbol = result["provider_symbol"]
    mapping.exchange_mic = result["exchange_mic"]
    mapping.provider_exchange = result["provider_exchange"]
    mapping.currency = result["currency"]
    mapping.isin = result["isin"]
    mapping.figi = result["figi"]
    mapping.metadata = result["metadata"]
    mapping.status = "VERIFIED"
    mapping.verification_method = method
    mapping.verified_at = timezone.now()
    mapping.last_error = ""
    try:
        mapping.save()
    except IntegrityError:
        mapping.status = "AMBIGUOUS"
        mapping.verification_method = ""
        mapping.verified_at = None
        mapping.last_error = "Finnhub symbol is already verified for another canonical instrument"
        mapping.save()
    return mapping


def verify_finnhub_mapping(instrument, *, client=None, provider_symbol=None, method="AUTOMATIC"):
    mapping, _ = InstrumentProviderMapping.objects.get_or_create(instrument=instrument, provider="FINNHUB")
    contract = getattr(instrument, "broker_contract", None)
    if instrument.asset_class.upper() not in settings.FINNHUB_SUPPORTED_ASSET_CLASSES or not contract:
        mapping.status = "UNSUPPORTED"
        mapping.last_error = ("Only qualified IBKR stock contracts are eligible" if contract
                              else "A qualified IBKR contract is required")
        mapping.verified_at = None
        mapping.save(update_fields=["status", "last_error", "verified_at", "updated_at"])
        return mapping
    active_client = client or FinnhubClient()
    query = provider_symbol or contract.local_symbol or instrument.symbol
    try:
        candidates = active_client.search_symbols(query)
        requested = _normalized_symbol(provider_symbol) if provider_symbol else None
        exact = [item for item in candidates if _normalized_symbol(item["provider_symbol"]) == requested] if requested else [
            item for item in candidates
            if _normalized_symbol(item["provider_symbol"]) in {
                _normalized_symbol(instrument.symbol), _normalized_symbol(contract.local_symbol)
            }
        ]
        # Finnhub may return duplicate search rows for the same exact listing.  They
        # are one identity, not multiple competing mappings; retain the row with the
        # richest exchange/currency evidence and keep genuine distinct symbols separate.
        deduplicated = {}
        for item in exact:
            key = _normalized_symbol(item.get("provider_symbol"))
            evidence = sum(bool(item.get(field)) for field in ("mic", "currency", "figi", "isin"))
            current = deduplicated.get(key)
            if current is None or evidence > current[0]:deduplicated[key] = (evidence, item)
        exact = [item for _, item in deduplicated.values()]
        valid = []
        rejected = []
        for candidate in exact:
            try:
                profile = active_client.profile(candidate["provider_symbol"])
            except ProviderError as exc:
                rejected.append({"provider_symbol": candidate["provider_symbol"], "reason": exc.code})
                continue
            result, reason = _validated_candidate(instrument, contract, candidate, profile)
            if result:
                valid.append(result)
            else:
                rejected.append({"provider_symbol": candidate["provider_symbol"], "reason": reason})
        mapping.metadata = {"candidate_count": len(candidates), "exact_count": len(exact), "rejected": rejected}
        mapping.verified_at = None
        mapping.verification_method = ""
        if len(valid) == 1:
            return _store_result(mapping, valid[0], method=method)
        mapping.status = "AMBIGUOUS" if len(valid) > 1 else "UNSUPPORTED"
        mapping.last_error = ("Multiple Finnhub symbols match the qualified IBKR contract" if len(valid) > 1
                              else rejected[0]["reason"] if len(rejected) == 1
                              else "No Finnhub symbol had matching stock, currency, and exchange evidence")
        mapping.save()
        return mapping
    except ProviderError as exc:
        mapping.status = "ERROR"
        mapping.verified_at = None
        mapping.verification_method = ""
        mapping.last_error = f"{exc.code}: {exc}"[:1000]
        mapping.save()
        return mapping


def manually_verify_finnhub_mapping(instrument, provider_symbol, *, client=None):
    symbol = str(provider_symbol or "").strip()
    if not symbol:
        raise ValueError("provider_symbol is required")
    return verify_finnhub_mapping(instrument, client=client, provider_symbol=symbol, method="MANUAL")


def verified_finnhub_mapping(instrument):
    return InstrumentProviderMapping.objects.filter(
        instrument=instrument, provider="FINNHUB", status="VERIFIED",
    ).exclude(provider_symbol="").first()


def fallback_eligibility(subscription, *, historical=False):
    if not settings.MARKET_DATA_FALLBACK_ENABLED:
        return False, "MARKET_DATA_FALLBACK_DISABLED", None
    if historical and not settings.FINNHUB_HISTORICAL_FALLBACK_ENABLED:
        return False, "FINNHUB_HISTORICAL_FALLBACK_DISABLED", None
    if not historical and not settings.FINNHUB_LIVE_FALLBACK_ENABLED:
        return False, "FINNHUB_LIVE_FALLBACK_DISABLED", None
    instrument = subscription.instrument
    if not instrument.active or not instrument.tradable:
        return False, "INSTRUMENT_NOT_TRADABLE", None
    if instrument.asset_class.upper() not in settings.FINNHUB_SUPPORTED_ASSET_CLASSES:
        return False, ProviderErrorCode.UNSUPPORTED_INSTRUMENT, None
    mapping = verified_finnhub_mapping(instrument)
    if not mapping:
        return False, ProviderErrorCode.FINNHUB_MAPPING_INVALID, None
    if not getattr(instrument, "broker_contract", None) or subscription.conid != instrument.broker_contract.conid:
        return False, "CANONICAL_CONTRACT_MISMATCH", None
    return True, "", mapping
