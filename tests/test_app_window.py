from delta.daemon import _is_delta_title


def test_finds_the_app_window_by_its_titles():
    assert _is_delta_title("Delta")
    assert _is_delta_title("Play seetharamam song · Delta")
    assert _is_delta_title("(2) Approval needed · Delta")
    assert not _is_delta_title("Delta files")  # the save folder's Explorer window
    assert not _is_delta_title("Claude")


def test_only_one_launcher_starts_the_service(tmp_path):
    """A second click while Delta starts used to start a second service, which crashed and wiped the log."""
    from delta.daemon import _launch_lock

    with _launch_lock(tmp_path) as first, _launch_lock(tmp_path) as second:
        assert first and not second
    with _launch_lock(tmp_path) as again:
        assert again  # released afterwards


def test_splash_page_carries_no_token():
    from delta.daemon import SPLASH

    assert "token" not in SPLASH.lower() and "<title>Delta</title>" in SPLASH
