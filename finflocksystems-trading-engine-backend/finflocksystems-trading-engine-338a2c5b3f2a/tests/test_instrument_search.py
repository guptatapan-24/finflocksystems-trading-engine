import pytest
from apps.instruments.models import BrokerContract, Instrument, OptionContract
from apps.instruments.services import option_chain, qualify_option_contract, resolve_instrument, search_broker_instruments
from apps.audit.models import OutboxEvent

pytestmark=pytest.mark.django_db


class BrokerStub:
    result={"symbol":"BHP","local_symbol":"BHP","conid":12345,"asset_class":"STK","exchange":"SMART",
        "primary_exchange":"ASX","currency":"AUD","description":"BHP Group Limited"}
    def search_contracts(self, query):return [{**self.result},{**self.result,"conid":67890,"primary_exchange":"LSE","currency":"GBP"}]
    def qualify_contract_exact(self, payload, key):return {**self.result,"conid":payload["conid"],"qualified":True}


def test_search_returns_multiple_unseeded_exact_contracts():
    rows=search_broker_instruments("BHP",BrokerStub())
    assert len(rows)==2 and {row["primary_exchange"] for row in rows}=={"ASX","LSE"}
    assert all(row["instrument_id"] is None for row in rows)


def test_search_rejects_queries_too_short_for_ibkr_symbol_matching():
    with pytest.raises(ValueError, match="at least 2"):
        search_broker_instruments("A",BrokerStub())


def test_selected_conid_is_qualified_and_persisted_exactly():
    row=BrokerStub.result
    instrument,contract,command=resolve_instrument(ticker=row["symbol"],asset_class=row["asset_class"],exchange=row["exchange"],
        primary_exchange=row["primary_exchange"],currency=row["currency"],conid=row["conid"],local_symbol=row["local_symbol"],
        description=row["description"],gateway=BrokerStub())
    assert command is None and contract.conid==row["conid"] and contract.description==row["description"]
    assert instrument.primary_exchange=="ASX" and BrokerContract.objects.get(conid=12345).instrument==instrument
    assert Instrument.objects.count()==1
    registry=OutboxEvent.objects.get(topic="instrument.registry.v1")
    assert registry.payload["conid"]==12345 and registry.payload["instrument_id"]==instrument.pk


def test_existing_selected_conid_is_requalified_from_broker():
    instrument=Instrument.objects.create(symbol="BHP",asset_class="STK",exchange="SMART",currency="AUD")
    BrokerContract.objects.create(instrument=instrument,conid=12345,local_symbol="BHP",qualified_at=None)
    resolved,contract,command=resolve_instrument(
        instrument_id=instrument.pk,conid=12345,primary_exchange="ASX",local_symbol="BHP",
        description="BHP Group Limited",gateway=BrokerStub(),
    )
    assert command is None and resolved==instrument
    assert contract.qualified_at is not None and contract.primary_exchange=="ASX"
    assert contract.description=="BHP Group Limited"


def test_qualification_idempotency_key_includes_complete_contract_payload():
    keys=[]

    class RecordingBroker(BrokerStub):
        def qualify_contract_exact(self,payload,key):
            keys.append(key)
            return {**self.result,"conid":payload["conid"],"description":payload["description"],"qualified":True}

    gateway=RecordingBroker()
    common={"ticker":"BHP","asset_class":"STK","exchange":"SMART","currency":"AUD",
            "conid":12345,"local_symbol":"BHP","gateway":gateway}
    resolve_instrument(primary_exchange="ASX",description="BHP Group Limited",**common)
    resolve_instrument(primary_exchange="ASX",description="BHP Group Limited ADR",**common)

    assert len(keys)==2
    assert keys[0] != keys[1]
    assert all(key.startswith("qualify:contract:") for key in keys)


def test_indian_contract_uses_indian_currency_and_trading_calendar():
    row={"symbol":"KISSHT","local_symbol":"KISSHT","conid":54321,"asset_class":"STK",
         "exchange":"NSE","primary_exchange":"NSE","currency":"INR","description":"Kissht Limited"}

    class IndianBroker:
        def qualify_contract_exact(self,payload,key):return {**row,"qualified":True}

    instrument,contract,command=resolve_instrument(
        ticker=row["symbol"],asset_class=row["asset_class"],exchange=row["exchange"],
        primary_exchange=row["primary_exchange"],currency=row["currency"],conid=row["conid"],
        local_symbol=row["local_symbol"],description=row["description"],gateway=IndianBroker())
    assert command is None and contract.conid==54321
    assert instrument.currency=="INR" and instrument.primary_exchange=="NSE"
    assert instrument.trading_calendar=="XNSE"


def test_indian_option_search_and_exact_qualification_persists_contract_identity():
    row={"symbol":"NIFTY","local_symbol":"NIFTY26AUG25000CE","conid":7654321,
         "asset_class":"OPT","exchange":"NFO","primary_exchange":"NSE","currency":"INR",
         "description":"NIFTY 26 Aug 2026 25000 Call","expiration":"20260826","strike":"25000",
         "right":"C","multiplier":"75","trading_class":"NIFTY","underlying_conid":1234}

    class IndianOptionBroker:
        def search_contracts(self,query,**filters):
            assert filters=={"asset_classes":("STK","OPT"),"country":"IN","currency":"INR"}
            return [row]
        def qualify_contract_exact(self,payload,key):
            assert payload["conid"]==row["conid"]
            return {**row,"qualified":True}

    results=search_broker_instruments("NIFTY",IndianOptionBroker(),country="IN",currency="INR")
    assert results==[{
        "symbol":"NIFTY","local_symbol":"NIFTY26AUG25000CE","conid":7654321,
        "asset_class":"OPT","exchange":"NFO","primary_exchange":"NSE","currency":"INR",
        "description":"NIFTY 26 Aug 2026 25000 Call","instrument_id":None,
        "expiration":"2026-08-26","strike":"25000","right":"C","multiplier":"75",
        "trading_class":"NIFTY","underlying_conid":1234,
    }]
    selected=results[0]
    instrument,contract,command=resolve_instrument(
        ticker=selected["symbol"],gateway=IndianOptionBroker(),qualify=True,**{
            key:value for key,value in selected.items() if key not in {"symbol","instrument_id"}
        })
    option=OptionContract.objects.get(instrument=instrument)
    assert command is None and contract.conid==7654321
    assert instrument.symbol=="NIFTY26AUG25000CE" and instrument.asset_class=="OPT"
    assert instrument.currency=="INR" and instrument.multiplier==75
    assert option.expiration.isoformat()=="2026-08-26" and option.strike==25000
    assert option.right=="C" and option.trading_class=="NIFTY" and option.underlying_conid==1234


def test_two_stage_index_option_chain_and_exact_qualification():
    underlying=Instrument.objects.create(symbol="NIFTY",asset_class="IND",exchange="NSE",primary_exchange="NSE",currency="INR",trading_calendar="XNSE")
    BrokerContract.objects.create(instrument=underlying,conid=1234,primary_exchange="NSE",local_symbol="NIFTY 50")

    class OptionGateway:
        def option_chain(self,payload):
            assert payload["underlying_conid"]==1234 and payload["asset_class"]=="IND"
            return {"chains":[{"exchange":"NFO","trading_class":"NIFTY","multiplier":"75",
                "expirations":["20260826","2026-09-30"],"strikes":[25100,25000]}]}
        def qualify_contract_exact(self,payload,key):
            assert payload["underlying_conid"]==1234 and payload["expiration"]=="2026-08-26"
            return {**payload,"conid":7654321,"local_symbol":"NIFTY26AUG25000CE",
                "primary_exchange":"NSE","description":"NIFTY 26 Aug 2026 25000 Call"}

    chain=option_chain(underlying_instrument=underlying,gateway=OptionGateway())
    assert chain["chains"]==[{"exchange":"NFO","trading_class":"NIFTY","multiplier":"75",
        "expirations":["2026-08-26","2026-09-30"],"strikes":["25000","25100"]}]
    instrument,contract,details=qualify_option_contract(
        underlying_instrument=underlying,expiration="2026-08-26",strike="25000",right="C",
        multiplier="75",trading_class="NIFTY",exchange="NFO",gateway=OptionGateway())
    assert contract.conid==7654321 and instrument.symbol=="NIFTY26AUG25000CE"
    assert details.underlying==underlying and details.underlying_conid==1234
