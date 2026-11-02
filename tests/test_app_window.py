from delta.daemon import _is_delta_title


def test_finds_the_app_window_by_its_titles():
    assert _is_delta_title("Delta")
    assert _is_delta_title("Play seetharamam song · Delta")
    assert _is_delta_title("(2) Approval needed · Delta")
    assert not _is_delta_title("Delta files")  # the save folder's Explorer window
    assert not _is_delta_title("Claude")
