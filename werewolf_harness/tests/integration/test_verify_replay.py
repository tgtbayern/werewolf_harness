from werewolf_harness.app.runtime.table import TableConfig
from werewolf_harness.scripts.verify_replay import verify

from .helpers import until
from .test_runtime import _rt


async def test_finished_games_replay_to_the_same_outcome(settings, store, redis):
    rt = _rt(settings, store, redis)
    await rt.start(scan=False)
    games = [await rt.create(TableConfig(variant="8p", models=["mock"]), seed=s) for s in (31, 32, 33)]
    await until(lambda: all(g.t.state == "over" for g in games), 90)
    r = await verify(store)
    assert r["checked"] == 3 and r["consistent_rate"] == 1.0, r
    await rt.shutdown()
