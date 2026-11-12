from django.db import transaction

from apps.instruments.models import BrokerContract, Instrument, InstrumentProviderMapping
from apps.instruments.services import resolve_instrument

from ..enums import MappingStatus
from ..models import InstrumentClassification, ResearchUniverseMember


def _mapping_status(instrument):
    contract = BrokerContract.objects.filter(instrument=instrument, qualified_at__isnull=False).first()
    if contract:
        return MappingStatus.BROKER_QUALIFIED
    mapping = InstrumentProviderMapping.objects.filter(instrument=instrument, status="VERIFIED").first()
    if mapping:
        return MappingStatus.PROVIDER_VERIFIED
    return MappingStatus.INSTRUMENT_MAPPED


@transaction.atomic
def map_universe_member(member, *, create_unqualified=True):
    member = ResearchUniverseMember.objects.select_for_update().select_related("issuer").get(pk=member.pk)
    candidates = Instrument.objects.filter(issuer=member.issuer, active=True)
    instrument = candidates.filter(
        symbol=member.source_symbol, asset_class="STK", currency=member.currency
    ).first()
    if instrument is None:
        exact = Instrument.objects.filter(
            symbol=member.source_symbol,
            asset_class="STK",
            currency=member.currency,
            exchange=member.exchange_hint or "SMART",
        )
        if exact.count() == 1:
            instrument = exact.first()
        elif exact.count() > 1:
            member.mapping_status = MappingStatus.METADATA_ONLY
            member.mapping_notes = "Ambiguous exact instrument identity; operator review required"
            member.save(update_fields=["mapping_status", "mapping_notes"])
            return member
    if instrument is None and create_unqualified:
        instrument = Instrument.objects.create(
            issuer=member.issuer,
            symbol=member.source_symbol,
            asset_class="STK",
            exchange=member.exchange_hint or "SMART",
            currency=member.currency,
            active=True,
            tradable=True,
        )
    if instrument is None:
        member.mapping_status = MappingStatus.METADATA_ONLY
        member.mapping_notes = "No deterministic instrument match"
        member.save(update_fields=["mapping_status", "mapping_notes"])
        return member
    if instrument.issuer_id not in {None, member.issuer_id}:
        member.mapping_status = MappingStatus.REJECTED
        member.mapping_notes = "Exact symbol belongs to a different issuer"
        member.save(update_fields=["mapping_status", "mapping_notes"])
        return member
    if instrument.issuer_id is None:
        instrument.issuer = member.issuer
        instrument.save(update_fields=["issuer"])
    InstrumentProviderMapping.objects.get_or_create(
        instrument=instrument,
        provider="FINNHUB",
        defaults={"provider_symbol": member.source_symbol, "currency": member.currency, "status": "PENDING"},
    )
    member.instrument = instrument
    member.mapping_status = _mapping_status(instrument)
    member.mapping_notes = "Mapped deterministically; broker qualification remains a separate gate"
    member.save(update_fields=["instrument", "mapping_status", "mapping_notes"])
    InstrumentClassification.objects.filter(
        issuer=member.issuer, taxonomy_version=member.universe.dataset_version, instrument__isnull=True
    ).update(instrument=instrument)
    return member


def map_research_universe(universe, *, create_unqualified=True):
    counts = {value: 0 for value in MappingStatus.values}
    for member in universe.members.filter(active=True).select_related("issuer"):
        result = map_universe_member(member, create_unqualified=create_unqualified)
        counts[result.mapping_status] += 1
    return counts


def qualify_member_exact(member, *, conid, primary_exchange, local_symbol="", description="", gateway=None):
    """Operator-controlled exact qualification; this is never called by bundle import."""
    if not member.instrument_id:
        raise ValueError("Member must be instrument-mapped before broker qualification")
    original_instrument_id=member.instrument_id
    instrument, contract, command = resolve_instrument(
        instrument_id=member.instrument_id,
        conid=conid,
        primary_exchange=primary_exchange,
        local_symbol=local_symbol,
        description=description,
        qualify=True,
        gateway=gateway,
    )
    if command is not None or contract is None or int(contract.conid) != int(conid):
        raise ValueError("Exact IBKR qualification did not complete")
    if instrument.issuer_id not in (None,member.issuer_id):
        raise ValueError("Selected conId belongs to a canonical instrument for another issuer")
    if instrument.issuer_id is None:
        instrument.issuer_id=member.issuer_id;instrument.save(update_fields=["issuer"])
    notes=f"Exact IBKR contract qualified: conId {contract.conid}"
    ResearchUniverseMember.objects.filter(
        issuer_id=member.issuer_id,source_symbol=member.source_symbol,
    ).update(instrument=instrument,mapping_status=MappingStatus.BROKER_QUALIFIED,mapping_notes=notes)
    InstrumentClassification.objects.filter(
        issuer_id=member.issuer_id,instrument_id=original_instrument_id,
    ).update(instrument=instrument)
    member.refresh_from_db()
    return member
