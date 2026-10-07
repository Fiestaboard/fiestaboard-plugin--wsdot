"""Cache-key tests for wsdot: one cache entry per board geometry and display,
so panel boards of different sizes never share a frame."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.plugins.base import PluginResult


def key(plugin, board):
    return plugin._cache_key_for_board(board)


def _board(device_type, cols, rows, display_key=None):
    """A board stand-in. ``display`` is set only when a key is given, so the
    key works on cores whose BoardContext predates ``display``."""
    if display_key is None:
        return SimpleNamespace(device_type=device_type, rows=rows, cols=cols, display=None)
    return SimpleNamespace(
        device_type=device_type, rows=rows, cols=cols, display=SimpleNamespace(key=display_key)
    )


class TestCacheKey:
    """The key folds in a panel's size and the board's display, like core's
    ``PluginBase._cache_key``, while fixed-size and note-array keys stay put."""

    def test_panel_boards_of_different_sizes_get_different_keys(self, plugin):
        """LED and TV boards are all device_type "panel": a 16x10 Pixoo and a
        22x9 TV panel must not share one cache entry."""
        pixoo = _board("panel", cols=16, rows=10)
        tv = _board("panel", cols=22, rows=9)
        assert key(plugin, pixoo) != key(plugin, tv)

    def test_panel_whose_grid_changes_gets_a_new_key(self, plugin):
        """A Pixoo switched to large text goes from 16x10 to 10x8; output laid
        out for the old grid must not be served to the new one."""
        before = _board("panel", cols=16, rows=10)
        after = _board("panel", cols=10, rows=8)
        assert key(plugin, before) != key(plugin, after)

    def test_same_size_boards_on_different_displays_get_different_keys(self, plugin):
        """Two boards of one size can draw differently (split-flap vs LED)."""
        flap = _board("panel", cols=22, rows=6, display_key="flap")
        led = _board("panel", cols=22, rows=6, display_key="led")
        assert key(plugin, flap) != key(plugin, led)

    def test_fixed_size_and_note_array_keys_are_unchanged(self, plugin):
        """Existing keys must still hit for boards with no display profile."""
        assert key(plugin, _board("flagship", cols=22, rows=6)) == "flagship"
        assert key(plugin, _board("note", cols=15, rows=3)) == "note"
        assert key(plugin, _board("note_array", cols=30, rows=6)) == "note_array:30x6"

    def test_board_without_display_attribute_uses_size_key(self, plugin):
        """Cores whose BoardContext has no ``display`` field still work."""
        board = SimpleNamespace(device_type="panel", rows=10, cols=16)
        assert key(plugin, board) == "panel:16x10"


def test_second_panel_size_is_not_served_the_first_panels_cached_data(plugin):
    """Data cached for a 16x10 Pixoo (headers and summary cut to 16 columns)
    must not be served to a 22x9 TV panel."""
    pixoo = _board("panel", cols=16, rows=10)
    plugin._cache[plugin._cache_key_for_board(pixoo)] = {"routes": [], "formatted": "PIXOO"}
    plugin.fetch_data = Mock(return_value=PluginResult(available=False, error="offline"))

    with plugin._bound_board(_board("panel", cols=22, rows=9)):
        assert plugin.get_formatted_display() is None
    plugin.fetch_data.assert_called_once()
