# Third-party components

## replayviewer-js (modified)

`web/replayviewer/index.js` and `web/replayviewer/stretch-worker.js` are the built
bundle of [daladal/replayviewer-js](https://github.com/daladal/replayviewer-js),
vendored from the npm package `replayviewer-js@1.0.0` (`dist/`).

**We ship a modified copy.** The cursor overlay additions were edited directly in the
built bundle (there is no upstream source tree here), so upgrading replayviewer-js means
replaying these changes:

- `Renderer` default options gain `showCursorTrace`, `cursorTraceMs`, `showSkinTrail`,
  `showClickMarkers`, `clickMarkerMs`, `clickMarkerShape`;
- new `drawCursorTrace()`, `getClickMarkers()`, `drawClickMarkers()`,
  `strokeClickMarker()` and the `_clickMarkers` cache, called from
  `drawAboveStoryboard()`;
- `drawCursor()` takes a `showSkinTrail` flag.

`web/skins/default/` is the default skin shipped with the same repository.

Upstream license and terms: see the replayviewer-js repository.

## osu! assets

`web/skins/default/` (and the hitsound samples inside it) come from osu!'s default skin
resources. Anyone publishing this project is responsible for checking osu!'s asset terms.

## LZMA

The replayviewer-js bundle embeds an LZMA decoder for the `.osr` frame data; see the
upstream bundle and its dependencies.
