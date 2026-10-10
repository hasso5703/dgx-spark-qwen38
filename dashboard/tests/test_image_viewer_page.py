#!/usr/bin/env python3
"""The Image view's result, shown at its own size and opened at full size, and the Video view's
at its own size (Hasan, 2026-10-10: the generated image did not show at its dimensions in the
cockpit, and could not be seen large). Same harness as test_page_behaviour.py: the page's scripts on a DOM built from its
own markup, so these hold what the scripts set; what a browser then lays out is measured on
the reference box (docs/image-lane.md).
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_behaviour import run, css_rules  # noqa: E402

PNG = "iVBORw0KGgo"            # what the lane answers, base64, cut short: the page never decodes it
SHOW = r"""
$('img-size').value = '%s'; imgSync();
imgShow({data: %s, inference_time_s: 7.5}, 'png');
const sc = $('img-screen'), im = sc.querySelector('img');
"""


def one(size="768x1376"):
    return SHOW % (size, "[{b64_json: '%s'}]" % PNG)


class TheResultTakesItsOwnShape(unittest.TestCase):
    def test_a_portrait_shows_as_a_portrait_not_in_a_wide_box(self):
        """Before: the screen went back to its 16:9 box as soon as the image arrived, and a
        9:16 image sat small in the middle of it."""
        r = run(self, one("768x1376") + r"""
        report({ratio: sc.style.aspectRatio, max: sc.style.maxWidth, fit: sc.classList.contains('fit')});
        """)
        self.assertEqual(r["ratio"], "768 / 1376")
        self.assertEqual(r["max"], "calc(78vh * 0.5581)")
        self.assertTrue(r["fit"])

    def test_the_image_corrects_the_box_once_it_says_its_own_size(self):
        r = run(self, one("1024x1024") + r"""
        im.naturalWidth = 1376; im.naturalHeight = 768; im.dispatchEvent({type: 'load'});
        report({ratio: sc.style.aspectRatio, max: sc.style.maxWidth});
        """)
        self.assertEqual(r, {"ratio": "1376 / 768", "max": "calc(78vh * 1.7917)"})

    def test_while_it_works_the_box_has_the_shape_asked_for(self):
        r = run(self, r"""
        $('img-size').value = '832x1248'; imgSync(); imgWorking('denoising', 40);
        const sc = $('img-screen');
        report({ratio: sc.style.aspectRatio, max: sc.style.maxWidth, fit: sc.classList.contains('fit')});
        """)
        self.assertEqual(r, {"ratio": "832 / 1248", "max": "calc(78vh * 0.6667)", "fit": True})

    def test_several_images_are_not_cut_by_the_box(self):
        """A gallery inside the 16:9 box was cut at its bottom (the box hides what overflows)."""
        r = run(self, SHOW % ("1024x1024", "[{b64_json: 'AAAA'}, {b64_json: 'BBBB'}, {b64_json: 'CCCC'}]") + r"""
        report({ratio: sc.style.aspectRatio, max: sc.style.maxWidth, n: sc.querySelectorAll('.gallery img').length});
        """)
        self.assertEqual(r, {"ratio": "auto", "max": "", "n": 3})

    def test_the_box_fills_the_column_and_is_centred_in_it(self):
        rules = {sel: decl for sel, media, decl in css_rules() if not media}
        fit = rules.get(".screen.fit", "")
        self.assertIn("width:100%", fit.replace(" ", ""))
        self.assertIn("align-self:center", fit.replace(" ", ""))


class ItOpensAtFullSize(unittest.TestCase):
    def test_a_click_on_the_image_opens_it_large(self):
        r = run(self, one("768x1376") + r"""
        im.click(); await __advance(1);    // the focus moves once the viewer shows
        report({open: !$('viewer').hidden, src: $('viewer-img').src, size: txt('viewer-size'),
                dl: $('viewer-dl').getAttribute('download'), focus: document.activeElement && document.activeElement.id});
        """)
        self.assertTrue(r["open"])
        self.assertEqual(r["src"], "data:image/png;base64," + PNG)
        self.assertEqual(r["size"], "768 × 1376")
        self.assertTrue(r["dl"].endswith(".png"), r["dl"])
        self.assertEqual(r["focus"], "viewer-close")

    def test_a_button_opens_it_too(self):
        r = run(self, one("1024x1024") + r"""
        const b = [...$('img-meta').querySelectorAll('button')].find(x => x.textContent === 'Enlarge');
        b.click(); report({button: !!b, open: !$('viewer').hidden});
        """)
        self.assertEqual(r, {"button": True, "open": True})

    def test_escape_closes_it_and_gives_the_focus_back(self):
        r = run(self, one("1024x1024") + r"""
        const b = [...$('img-meta').querySelectorAll('button')].find(x => x.textContent === 'Enlarge');
        b.focus(); b.click();
        document.dispatchEvent({type: 'keydown', key: 'Escape', preventDefault(){}});
        report({open: !$('viewer').hidden, back: document.activeElement === b});
        """)
        self.assertEqual(r, {"open": False, "back": True})

    def test_a_click_beside_the_image_closes_it(self):
        r = run(self, one("1024x1024") + r"""
        im.click(); $('viewer').dispatchEvent({type: 'click', target: $('viewer')});
        report(!$('viewer').hidden);
        """)
        self.assertFalse(r)

    def test_actual_pixels_shows_one_image_pixel_per_screen_pixel(self):
        """Fitted, a 2048 image on a 1000-pixel-high window is shrunk; at 100 % it is drawn
        at its own pixels, the screen's, whatever the display's density."""
        r = run(self, one("2048x2048") + r"""
        window.devicePixelRatio = 2; im.click();
        const v = $('viewer-img'); v.naturalWidth = 2048; v.naturalHeight = 2048; v.dispatchEvent({type: 'load'});
        $('viewer-actual').click();
        const on = {pressed: $('viewer-actual').getAttribute('aria-pressed'), cls: $('viewer-pane').classList.contains('actual'), w: v.style.width};
        $('viewer-actual').click();
        report({on, off: {pressed: $('viewer-actual').getAttribute('aria-pressed'), cls: $('viewer-pane').classList.contains('actual'), w: v.style.width}});
        """)
        self.assertEqual(r["on"], {"pressed": "true", "cls": True, "w": "1024px"})
        self.assertEqual(r["off"], {"pressed": "false", "cls": False, "w": ""})

    def test_the_viewer_fits_its_picture_to_the_window(self):
        """Measured in a browser (2026-10-10): with the grid's auto track the picture's 100 %
        caps resolved against its own size, and a 1456x1920 portrait ran 1920 pixels down a
        1000-pixel window; one track the size of the pane brings it to 704x928."""
        rules = {sel: decl.replace(" ", "") for sel, media, decl in css_rules() if not media}
        self.assertIn("grid-template:minmax(0,1fr)/minmax(0,1fr)", rules.get(".viewer-pane", ""))
        self.assertIn("max-height:100%", rules.get(".viewer-pane img", ""))
        self.assertIn("place-items:safecenter", rules.get(".viewer-pane.actual", ""))

    def test_each_image_of_a_gallery_opens_its_own(self):
        r = run(self, SHOW % ("1024x1024", "[{b64_json: 'AAAA'}, {b64_json: 'BBBB'}]") + r"""
        sc.querySelectorAll('.gallery img')[1].click();
        report($('viewer-img').src);
        """)
        self.assertEqual(r, "data:image/png;base64,BBBB")

    def test_the_session_history_shows_each_image_whole(self):
        """A thumbnail of a portrait cut to a square hid most of it."""
        r = run(self, one("768x1376") + r"""
        const t = $('img-history').querySelector('img');
        report(t.style.objectFit);
        """)
        self.assertEqual(r, "contain")


class TheVideoTakesItsOwnShape(unittest.TestCase):
    """The same 16:9 box held the Video view's 9:16 videos small in its middle."""

    def test_a_portrait_video_shows_as_a_portrait_then_in_its_own_size(self):
        r = run(self, r"""
        VS.size = '480x864'; const v = vidScreenPlay('vid-1'); const sc = $('vid-screen');
        const asked = {ratio: sc.style.aspectRatio, max: sc.style.maxWidth, fit: sc.classList.contains('fit')};
        v.videoWidth = 1280; v.videoHeight = 704; v.dispatchEvent({type: 'loadedmetadata'});
        const own = sc.style.aspectRatio;
        vidScreenEmpty();
        report({asked, own, empty: [sc.style.aspectRatio, sc.style.maxWidth, sc.classList.contains('fit')]});
        """)
        self.assertEqual(r["asked"], {"ratio": "480 / 864", "max": "calc(78vh * 0.5556)", "fit": True})
        self.assertEqual(r["own"], "1280 / 704")
        self.assertEqual(r["empty"], ["", "", False])

    def test_while_the_lane_works_the_screen_has_the_shape_asked_for(self):
        r = run(self, r"""
        VS.size = '480x864'; vidScreenWorking('denoise', 30, '3', 'of 9'); const sc = $('vid-screen');
        report([sc.style.aspectRatio, sc.classList.contains('fit')]);
        """)
        self.assertEqual(r, ["480 / 864", True])


if __name__ == "__main__":
    unittest.main()
