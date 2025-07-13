from server_bot import tariff_info, set_tariff, add_daily_gen

def test_free_limit():
    info = tariff_info("dummy")   # несуществующий → free
    assert info == {"slots": 1, "daily_gen": 5}

def test_vip_limit():
    set_tariff("dummy", "vip")
    assert tariff_info("dummy")["slots"] == 6


def test_bonus_limit_increases():
    uid = "bonus"
    set_tariff(uid, "free")
    before = tariff_info(uid)["daily_gen"]
    add_daily_gen(uid, 3)
    after = tariff_info(uid)["daily_gen"]
    assert after == before + 3
