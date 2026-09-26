"""Board-geometry conformance for the WSDOT (Washington State Ferries) plugin.

Renders the plugin across every board shape FiestaBoard supports -- Flagship,
Note, and Note arrays from 15x3 up to 120x24 (which is also what a FiestaPanel
is) -- and asserts it never overflows a row or column, and that its ferry
departure list actually grows on a taller board rather than staying capped at
a Flagship's fixed 6 rows. See ``src/plugins/geometry_conformance.py`` in
FiestaBoard core for what is checked.
"""

import json
from pathlib import Path
from unittest.mock import Mock

from src.plugins.geometry_conformance import assert_board_conformance

from plugins.wsdot import WsdotPlugin

MANIFEST = json.loads((Path(__file__).parent.parent / "manifest.json").read_text())


def _schedule_sailings(count: int = 24) -> list:
    """*count* sailings alternating direction -- enough that even a 24-row
    max note_array has more departures to reflow into than a Note (3 rows)
    or Flagship (6 rows) can ever show, per route.
    """
    sailings = []
    for i in range(count):
        hour = 6 + (i // 2)
        minute = "00" if i % 2 == 0 else "30"
        sailings.append(
            {
                "DepartureTime": f"{hour:02d}:{minute}",
                "VesselID": 1 + (i % 3),
                "SpacesLeft": 40 + i,
                "Direction": "A" if i % 2 == 0 else "B",
            }
        )
    return sailings


def _get_side_effect(url, params=None, **kwargs):
    if "scheduletoday" in url:
        return Mock(status_code=200, json=lambda: _schedule_sailings(), raise_for_status=Mock())
    if "vesselbasics" in url:
        return Mock(
            status_code=200,
            json=lambda: [
                {"VesselID": 1, "VesselName": "Wenatchee"},
                {"VesselID": 2, "VesselName": "Tacoma"},
                {"VesselID": 3, "VesselName": "Kaleetan"},
            ],
            raise_for_status=Mock(),
        )
    if "alerts" in url:
        return Mock(
            status_code=200,
            json=lambda: [{"Headline": "Weather delay", "AlertFullDescription": "Expect delays"}],
            raise_for_status=Mock(),
        )
    if "terminalsailingspace" in url or "terminalwaittimes" in url:
        return Mock(status_code=200, json=lambda: [], raise_for_status=Mock())
    return Mock(status_code=200, json=lambda: [], raise_for_status=Mock())


def make_plugin() -> WsdotPlugin:
    """A fresh, configured WsdotPlugin with the network fully stubbed.

    Two routes, each with 24 mocked sailings (12 per direction) and one
    active alert, so every board shape -- including the 120x24 max
    note_array -- has real content to reflow into rather than the suite
    measuring an artificially small fixture.
    """
    plugin = WsdotPlugin(MANIFEST)
    plugin.config = {
        "api_access_code": "test-code",
        "routes": [{"route_id": 7}, {"route_id": 9}],
    }
    plugin._get = lambda base, path, params=None: _get_side_effect(f"{base}/{path}").json()
    return plugin


def test_renders_on_every_board_shape():
    """The plugin must fit, and grow into, every board shape the platform supports.

    ``strict_growth=True``: this plugin renders a ferry departure list, and
    with 24 sailings per route always available, a taller board that fills
    every row has strictly more departures to show -- unlike a clock or a
    single status line, this is not "genuinely capped" content.
    """
    assert_board_conformance(
        make_plugin,
        manifest=MANIFEST,
        strict_growth=True,
        require_note_array_preview=True,
    )
