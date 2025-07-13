import server_bot as sb

def test_toggle_filter(monkeypatch, tmp_path):
    db = tmp_path / "settings.json"
    db.write_text("{}")
    monkeypatch.setattr(sb, "SETTINGS_DB", str(db))

    on = sb.toggle_filter("u1")
    assert on is True
    assert sb.load_json(sb.SETTINGS_DB)["u1"]["filter_off"] is True

    off = sb.toggle_filter("u1")
    assert off is False
    assert sb.load_json(sb.SETTINGS_DB)["u1"]["filter_off"] is False
