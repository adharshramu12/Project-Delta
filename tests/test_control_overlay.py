"""The overlay's geometry and colours (pure, every platform). The real Win32 windows are checked on a Windows
desktop in tests/e2e/test_control_overlay_e2e.py."""

from __future__ import annotations

import sys

from delta.system import control_overlay as co


def test_ring_is_centred_on_the_pointer_and_scales_with_dpi():
    left, top, size, pen = co.ring_box(500, 300, 96)
    assert (left + size // 2, top + size // 2) == (500, 300) and pen >= 2
    _, _, big, big_pen = co.ring_box(500, 300, 144)  # 150 % scaling
    assert big == round(size * 1.5) and big_pen > pen
    _, _, _, hc_pen = co.ring_box(500, 300, 96, high_contrast=True)
    assert hc_pen > pen  # thicker in high contrast


def test_border_is_four_thin_strips_inside_each_monitor():
    rect = (-1920, 0, 0, 1080)  # a second monitor left of the main one
    strips = co.border_strips(rect, 96)
    assert len(strips) == 4
    for x, y, w, h in strips:
        assert x >= -1920 and x + w <= 0 and y >= 0 and y + h <= 1080
        assert min(w, h) <= 4  # thin: never covers what the user is looking at
    assert min(min(w, h) for *_, w, h in co.border_strips(rect, 192)) > min(min(w, h) for *_, w, h in strips)


def test_label_sits_centred_at_the_top():
    x, y, w, _h = co.label_box((0, 0, 1920, 1080), 96)
    assert abs((x + w / 2) - 960) <= 1 and 0 < y < 40 and w < 1920


def test_orange_normally_and_windows_colours_in_high_contrast():
    assert co.style_for(False).ring == co.ORANGE == (249, 115, 22)
    hc = co.style_for(True, (0, 255, 255), (0, 0, 0))
    assert hc.ring == (0, 255, 255) and hc.text == (0, 0, 0) and hc.high_contrast
    assert co.rgb_of(co.colorref((1, 2, 3))) == (1, 2, 3)


def test_click_through_never_takes_focus_and_stays_on_top():
    s = co.OVERLAY_EX_STYLE
    for bit in (co.WS_EX_LAYERED, co.WS_EX_TRANSPARENT, co.WS_EX_TOOLWINDOW, co.WS_EX_NOACTIVATE, co.WS_EX_TOPMOST):
        assert s & bit


def test_label_tells_how_to_stop():
    assert "Esc twice" in co.LABEL_TEXT and "stop" in co.LABEL_TEXT


def test_off_windows_it_is_a_quiet_no_op():
    if sys.platform == "win32":
        return
    o = co.make_overlay()
    assert isinstance(o, co.NullOverlay) and not o.start()
    o.show()
    o.beat()
    with o.hidden_for_capture():
        pass
    o.hide()
    o.close()
