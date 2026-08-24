from app.web import NAV_ITEMS, WEB_DIR


def test_web_ui_assets_are_present():
    assert (WEB_DIR / "templates" / "dashboard.html").is_file()
    assert (WEB_DIR / "templates" / "resource.html").is_file()
    assert (WEB_DIR / "static" / "app.css").is_file()


def test_navigation_has_no_duplicate_routes():
    paths = [path for path, _label in NAV_ITEMS]
    assert paths[0] == "/"
    assert len(paths) == len(set(paths))
