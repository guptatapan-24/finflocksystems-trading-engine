import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.broker_gateway import qch as qch_module
from apps.broker_gateway.management.commands import qch_probe


class FakeClient:
    def __init__(self, error=None):
        self.created, self.deleted, self.error = [], [], error

    def list_containers(self):
        return [qch_module.QCHContainer(id="1", name="finflock-gateway-abc", status="RUNNING", raw={"env": {"IB_PASSWORD": "SECRET"}})]

    def create_container(self, **kw):
        if self.error:
            raise self.error
        self.created.append(kw)
        return qch_module.QCHContainer(id="2", name=kw["name"], status="STARTING", raw={})

    def delete_container(self, name):
        self.deleted.append(name)
        return True


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(qch_probe, "QCHBrokerClient", lambda: fake)
    return fake


def test_list_shows_names_and_statuses_only(client, capsys):
    call_command("qch_probe", "list")
    out = capsys.readouterr().out
    assert "finflock-gateway-abc  RUNNING" in out and "SECRET" not in out


def test_create_needs_yes_and_a_probe_name_and_an_image(client):
    with pytest.raises(CommandError):
        call_command("qch_probe", "create", "--image", "app:v1")
    with pytest.raises(CommandError):
        call_command("qch_probe", "create", "--image", "app:v1", "--name", "not-a-probe", "--yes")
    with pytest.raises(CommandError):
        call_command("qch_probe", "create", "--yes")
    with pytest.raises(CommandError):
        call_command("qch_probe", "create", "--image", "bad image;rm -rf", "--yes")
    assert client.created == []


def test_create_sends_no_environment_and_the_requested_command(client, settings):
    settings.QCH_SUBCONTAINER_NETWORK = "qch-net"
    call_command("qch_probe", "create", "--image", "trading_engine_20:v4", "--command", "sleep 30", "--yes")
    assert client.created == [{"name": "probe-sleep", "image": "trading_engine_20:v4", "command": ["sleep", "30"], "env": {}, "network": "qch-net"}]


def test_command_can_be_sent_as_a_string(client):
    call_command("qch_probe", "create", "--image", "docker.io/acme/x:1", "--command", "sleep 5", "--command-as-string", "--yes")
    assert client.created[0]["command"] == "sleep 5"


def test_delete_only_touches_probe_containers(client):
    with pytest.raises(CommandError):
        call_command("qch_probe", "delete", "--name", "finflock-gateway-abc", "--yes")
    call_command("qch_probe", "delete", "--name", "probe-sleep", "--yes")
    assert client.deleted == ["probe-sleep"]


def test_a_qch_refusal_is_reported_clearly(monkeypatch):
    monkeypatch.setattr(qch_probe, "QCHBrokerClient", lambda: FakeClient(error=qch_module.QCHError("nope", status_code=422)))
    with pytest.raises(CommandError, match="QCH said: nope .*422"):
        call_command("qch_probe", "create", "--image", "app:v1", "--yes")
