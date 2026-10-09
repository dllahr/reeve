import itertools

import pytest

from reeve import Store, Projection


class Ledger(Projection):
    """Test projection: running coin balance per holder. Stands in for the real
    ledger projection that will arrive with the ruleset-neutral container model."""

    name = "ledger"
    tables = ("p_balance",)

    def create(self, connection):
        connection.execute("CREATE TABLE IF NOT EXISTS p_balance("
                           "campaign_id TEXT, holder TEXT, coin INTEGER, PRIMARY KEY(campaign_id, holder))")

    def apply(self, connection, event):
        if event.type != "CoinTransferred":
            return
        payload = event.payload
        for holder, coin_change in ((payload["from"], -payload["amount"]), (payload["to"], payload["amount"])):
            connection.execute(
                "INSERT INTO p_balance VALUES (?,?,?) ON CONFLICT(campaign_id, holder)"
                " DO UPDATE SET coin = coin + excluded.coin", (event.campaign_id, holder, coin_change))


def make_ticking_clock():
    """A clock that advances one second per call, so exports are reproducible."""
    seconds = itertools.count(1)
    return lambda: f"2026-10-08T00:00:{next(seconds):02d}.000000Z"


@pytest.fixture
def ticking_clock():
    return make_ticking_clock()


@pytest.fixture
def store(ticking_clock):
    store = Store(":memory:", projections=[Ledger()], clock=ticking_clock)
    store.create_campaign("c1", "toy")
    return store


def pay(store, campaign_id, payer, payee, amount, actor_id="dm1"):
    with store.transaction(campaign_id, actor_id=actor_id, role="dm", command="pay") as transaction:
        transaction.emit("CoinTransferred", {"from": payer, "to": payee, "amount": amount},
                         subjects=[payer, payee], world_time=0)
