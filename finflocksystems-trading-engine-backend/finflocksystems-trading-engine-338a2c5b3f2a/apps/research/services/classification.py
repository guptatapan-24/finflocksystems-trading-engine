from django.db.models import Q

from ..models import InstrumentClassification


def classification_on(issuer, decision_date):
    """Return the classification knowable/effective on a decision date."""
    return (
        InstrumentClassification.objects.filter(
            issuer=issuer,
            effective_from__lte=decision_date,
        )
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=decision_date))
        .select_related("sub_industry_node__parent__parent__parent")
        .order_by("-effective_from")
        .first()
    )


def hierarchy(classification):
    if not classification:
        return {}
    sub = classification.sub_industry_node
    industry = sub.parent
    group = industry.parent if industry else None
    sector = group.parent if group else None
    return {
        "sector": {"code": sector.code, "name": sector.name} if sector else None,
        "industry_group": {"code": group.code, "name": group.name} if group else None,
        "industry": {"code": industry.code, "name": industry.name} if industry else None,
        "sub_industry": {"code": sub.code, "name": sub.name},
    }
