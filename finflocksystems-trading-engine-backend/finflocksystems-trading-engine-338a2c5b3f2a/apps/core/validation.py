from decimal import Decimal, InvalidOperation


class ValidationError(ValueError):
    pass


def decimal_field(payload,name,*,required=False,positive=False,allow_zero=True,max_digits=24,decimal_places=8):
    raw=payload.get(name)
    if raw in (None,""):
        if required:raise ValidationError(f"{name} is required")
        return None
    try:value=Decimal(str(raw))
    except (InvalidOperation,ValueError,TypeError) as exc:raise ValidationError(f"{name} must be a decimal number") from exc
    if not value.is_finite():raise ValidationError(f"{name} must be finite")
    if positive and (value<0 or (value==0 and not allow_zero)):raise ValidationError(f"{name} must be positive")
    sign,digits,exponent=value.as_tuple()
    fractional=max(-exponent,0);integer=max(len(digits)+exponent,0)
    if fractional>decimal_places:raise ValidationError(f"{name} supports at most {decimal_places} decimal places")
    if integer>max_digits-decimal_places or len(digits)>max_digits:
        raise ValidationError(f"{name} exceeds the supported precision")
    return value


def require_fields(payload,*names):
    missing=[name for name in names if payload.get(name) in (None,"")]
    if missing:raise ValidationError(f"Missing required fields: {', '.join(missing)}")


def validate_order_payload(payload,*,partial=False):
    if not isinstance(payload,dict):raise ValidationError("Request body must be a JSON object")
    allowed={"portfolio_id","instrument_id","side","quantity","order_type","limit_price","stop_price",
        "reference_price","time_in_force"}
    unknown=set(payload)-allowed
    if unknown:raise ValidationError(f"Unsupported order fields: {', '.join(sorted(unknown))}")
    if not partial:require_fields(payload,"portfolio_id","instrument_id","side","quantity")
    side=str(payload.get("side") or "").upper()
    if not partial and side not in {"BUY","SELL"}:raise ValidationError("side must be BUY or SELL")
    order_type=str(payload.get("order_type") or "MKT").upper()
    if order_type not in {"MKT","LMT","STP","STP_LMT"}:raise ValidationError("Unsupported order_type")
    tif=str(payload.get("time_in_force") or "DAY").upper()
    if tif not in {"DAY","GTC"}:raise ValidationError("time_in_force must be DAY or GTC")
    quantity=decimal_field(payload,"quantity",required=not partial,positive=True,allow_zero=False)
    limit_price=decimal_field(payload,"limit_price",positive=True,allow_zero=False)
    stop_price=decimal_field(payload,"stop_price",positive=True,allow_zero=False)
    reference_price=decimal_field(payload,"reference_price",positive=True,allow_zero=False)
    if order_type in {"LMT","STP_LMT"} and limit_price is None:raise ValidationError(f"{order_type} orders require limit_price")
    if order_type in {"STP","STP_LMT"} and stop_price is None:raise ValidationError(f"{order_type} orders require stop_price")
    if order_type=="MKT" and (limit_price is not None or stop_price is not None):
        raise ValidationError("MKT orders cannot include limit_price or stop_price")
    if order_type=="LMT" and stop_price is not None:
        raise ValidationError("LMT orders cannot include stop_price")
    if order_type=="STP" and limit_price is not None:
        raise ValidationError("STP orders cannot include limit_price")
    return {"side":side,"order_type":order_type,"time_in_force":tif,"quantity":quantity,
        "limit_price":limit_price,"stop_price":stop_price,"reference_price":reference_price}
