from types import SimpleNamespace

from PIL import ImageColor

from tools.vn_portrait_overlay_lite import overlay_class


def test_sweep_is_muted_once_without_changing_geometry_cadence_or_other_items(tmp_path):
    original_colors = ["#0c2b30", "#11373a", "#194440", "#28584f", "#153b3b"]
    items = {index: {"fill": color, "width": 1, "coords": (13, 40 + index, 456, 40 + index)}
             for index, color in enumerate(original_colors)}
    items[99] = {"fill": "#d6f4e9", "text": "字幕不变"}
    touched = []
    class OriginalWindow:
        def __init__(self):
            self._scan_lines = list(range(5))
            self.root = SimpleNamespace(bind=lambda *args, **kwargs: None)
            def configure(item, **values):
                touched.append((item, values))
                items[item].update(values)
            self.frame = SimpleNamespace(itemcget=lambda item, field: items[item][field], itemconfigure=configure)
        def _scan_tick(self):
            pass
    legacy = SimpleNamespace(PortraitOverlayTk=OriginalWindow, CARD_BG="#071e24")
    cls = overlay_class(legacy)
    cls(lite_dir=tmp_path)
    assert cls._scan_tick is OriginalWindow._scan_tick
    assert len(touched) == 5 and all(set(values) == {"fill"} for _, values in touched)
    background = ImageColor.getrgb(legacy.CARD_BG)
    for index, color in enumerate(original_colors):
        expected = tuple(round(bg + (fg - bg) * 0.25) for bg, fg in zip(background, ImageColor.getrgb(color)))
        assert ImageColor.getrgb(items[index]["fill"]) == expected
        assert items[index]["coords"] == (13, 40 + index, 456, 40 + index)
        assert items[index]["width"] == 1
    assert items[99] == {"fill": "#d6f4e9", "text": "字幕不变"}
