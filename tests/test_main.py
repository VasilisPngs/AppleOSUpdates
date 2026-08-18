"""python3 -m unittest discover -s tests"""

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class TestParsing(unittest.TestCase):
    def test_rss_titles(self):
        self.assertEqual(
            main.parse_rss_title("iOS 27.0 beta 5 (24A5408d)"),
            ("ios", "27.0", "beta5", "24A5408d"),
        )
        self.assertEqual(
            main.parse_rss_title("macOS Tahoe 27 beta 5 (24A5408d)"),
            ("macos", "27", "beta5", "24A5408d"),
        )
        self.assertEqual(
            main.parse_rss_title("iOS 26.7 Release Candidate (23U80)")[2], "rc"
        )
        # iPadOS is its own platform, and must not be truncated to iOS by
        # prefix order either.
        self.assertEqual(main.parse_rss_title("iPadOS 26.6 (23U67)")[0], "ipados")
        self.assertIsNone(main.parse_rss_title("TestFlight Update"))

    def test_only_operating_systems_are_tracked(self):
        """A browser and an IDE are not OSes; Apple ships both on these feeds."""
        for title in (
            "Safari 26.6 (21621.1.2)",
            "Safari Technology Preview 234 (Safari 26.0)",
            "Xcode 27 beta 5 (27A5237l)",
            "TestFlight Update",
            "App Store Connect Update",
        ):
            self.assertIsNone(main.parse_rss_title(title), title)

        for name in ("Safari 26.6", "Xcode 16.3"):
            self.assertIsNone(main.parse_security_name(name), name)

        self.assertEqual(
            list(main.LABELS),
            ["ios", "ipados", "macos", "tvos", "watchos", "visionos"],
        )

    def test_security_page_names(self):
        # Combined names must yield one release, not two.
        self.assertEqual(
            main.parse_security_name("iOS 26.6 and iPadOS 26.6"), ("ios", "26.6", "stable")
        )
        self.assertEqual(
            main.parse_security_name("macOS Tahoe 26.6.1"), ("macos", "26.6.1", "stable")
        )
        self.assertEqual(
            main.parse_security_name("Rapid Security Response macOS Ventura 13.4.1 (a)"),
            ("macos", "13.4.1 (a)", "rsr"),
        )
        self.assertIsNone(main.parse_security_name("Some unrelated advisory"))

    def test_appended_note_is_not_part_of_the_name(self):
        """A note running into the name breaks the version regex:
        '11.7.11This update...' matches as 11.7."""
        cell = (
            '<p class="gb-paragraph">macOS Big Sur 11.7.11</p>'
            '<div class="note gb-note"><p>This update has no published CVE entries.</p></div>'
        )
        self.assertEqual(main.cell_name(cell), "macOS Big Sur 11.7.11")
        self.assertEqual(
            main.parse_security_name(main.cell_name(cell)),
            ("macos", "11.7.11", "stable"),
        )

    def test_linked_name_uses_anchor_text(self):
        cell = '<p><a href="https://support.apple.com/en-us/128066">iOS 26.6 and iPadOS 26.6</a></p>'
        self.assertEqual(main.cell_name(cell), "iOS 26.6 and iPadOS 26.6")

    def test_gdmf_platforms_from_devices(self):
        """gdmf files watchOS and tvOS under 'iOS'; one asset can cover two."""
        self.assertEqual(main.platforms_of(["Watch6,1"], "ios"), ["watchos"])
        self.assertEqual(main.platforms_of(["AppleTV11,1", "AudioAccessory5,1"], "ios"), ["tvos"])
        self.assertEqual(main.platforms_of(["RealityDevice14,1"], "visionos"), ["visionos"])
        self.assertEqual(main.platforms_of(["J700AP"], "macos"), ["macos"])
        # A combined iOS/iPadOS asset must yield both, not just the first.
        self.assertEqual(
            main.platforms_of(["iPhone17,1", "iPad14,3"], "ios"), ["ios", "ipados"]
        )

    def test_combined_security_row_yields_both_platforms(self):
        """One row, two releases: their versions drift apart over time."""
        self.assertEqual(
            main.parse_security_names("iOS 26.6 and iPadOS 26.6"),
            [("ios", "26.6", "stable"), ("ipados", "26.6", "stable")],
        )
        self.assertEqual(
            main.parse_security_names("macOS Tahoe 26.6.1"),
            [("macos", "26.6.1", "stable")],
        )


class TestMerge(unittest.TestCase):
    def test_build_variants_are_one_release(self):
        """Apple ships iOS 26.6 as both 23G71 and 23G6071."""
        a = main.Release("ios", "26.6", "stable", build="23G71")
        b = main.Release("ios", "26.6", "stable", build="23G6071")
        self.assertEqual(a.key, b.key)

    def test_beta_numbers_stay_distinct(self):
        a = main.Release("ios", "27.0", "beta5")
        b = main.Release("ios", "27.0", "beta6")
        self.assertNotEqual(a.key, b.key)

    def test_merge_prefers_shorter_build_and_better_title(self):
        gdmf = main.Release("ios", "26.6", "stable", title="iOS 26.6", build="23G6071", source_rank=2)
        page = main.Release(
            "ios", "26.6", "stable", title="iOS 26.6 and iPadOS 26.6",
            build="23G71", source_rank=1,
        )
        gdmf.merge(page)
        self.assertEqual(gdmf.build, "23G71")
        self.assertEqual(gdmf.title, "iOS 26.6 and iPadOS 26.6")


class TestReRelease(unittest.TestCase):
    """Two builds at once is one release. A later build is a reissue."""

    def test_the_two_concurrent_builds_are_one_release(self):
        """23G6071 is 23G71 plus 6000: the same update, not a second one."""
        primary = main.Release("ios", "26.6", "stable", build="23G71")
        alternate = main.Release("ios", "26.6", "stable", build="23G6071")
        self.assertEqual(primary.key, alternate.key)
        primary.merge(alternate)
        self.assertEqual(primary.build, "23G71")  # shorter one wins

    def test_a_later_build_for_the_same_version_is_a_new_release(self):
        """23G73 after 23G71 is a different thing to install."""
        seen = {"ios|26.6|stable": "23G71"}
        reissued = main.Release("ios", "26.6", "stable", build="23G73")
        was = seen.get(reissued.key, "")
        self.assertTrue(bool(was) and bool(reissued.build) and was != reissued.build)

    def test_the_alternate_build_is_not_a_re_release(self):
        """Either build recorded, the other is still the same release."""
        self.assertTrue(main.same_build("23G71", "23G6071"))
        self.assertTrue(main.same_build("23U67", "23U6067"))
        self.assertTrue(main.same_build("22H352", "22H6352"))
        self.assertTrue(main.same_build("24A5408d", "24A11408d"))
        # A genuine reissue is not 6000 apart.
        self.assertFalse(main.same_build("23G71", "23G73"))
        self.assertFalse(main.same_build("23G71", "23H71"))

    def test_a_missing_build_is_not_a_re_release(self):
        """An empty build is a quiet source, not a reissue."""
        seen = {"ios|26.6|stable": "23G71"}
        quiet = main.Release("ios", "26.6", "stable", build="")
        was = seen.get(quiet.key, "")
        self.assertFalse(bool(was) and bool(quiet.build) and was != quiet.build)


class TestRender(unittest.TestCase):
    def test_layout(self):
        """Build belongs on the title line; the date sits under it."""
        from datetime import date

        release = main.Release(
            "ios", "27.0", "beta5", title="iOS 27.0 beta 5",
            build="24A5408d", released=date(2026, 8, 10),
        )
        lines = main.render(release).split("\n")
        self.assertEqual(lines, ["📱 <b>iOS 27.0 beta 5 (24A5408d)</b>", "10 August 2026"])
        self.assertNotIn("#", main.render(release))

    def test_betas_keep_their_platform_icon(self):
        """On a six-beta day the icon is what tells the platforms apart."""
        icons = {
            main.render(main.Release(family, "27.0", "beta5", title="x"))[0]
            for family in ("ios", "macos", "tvos", "watchos", "visionos")
        }
        self.assertEqual(len(icons), 5)

    def test_html_is_escaped(self):
        from datetime import date

        release = main.Release(
            "ios", "26.6", "stable", title="iOS <b>26.6</b> & more",
            build="23G71", released=date(2026, 7, 27),
        )
        text = main.render(release)
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("(23G71)", text)
        self.assertIn("27 July 2026", text)

    def test_release_without_build_has_no_empty_parens(self):
        release = main.Release("tvos", "26.6", "stable", title="tvOS 26.6")
        self.assertNotIn("()", main.render(release))

    def test_posts_carry_no_links(self):
        """The channel says an update exists; it does not send you reading."""
        from datetime import date

        release = main.Release("ios", "26.6", "stable", title="iOS 26.6",
                               build="23G71", released=date(2026, 7, 27))
        text = main.render(release)
        self.assertNotIn("<a ", text)
        self.assertNotIn("http", text)
        self.assertFalse(hasattr(main, "upgrade_links"))

    def test_every_release_stands_alone(self):
        """No grouping: there is exactly one layout, two lines long."""
        self.assertFalse(hasattr(main, "render_digest"))

        from datetime import date

        release = main.Release(
            "macos", "27.0", "beta5", title="macOS 27.0 beta 5",
            build="26A5406e", released=date(2026, 8, 10),
        )
        self.assertEqual(len(main.render(release).split("\n")), 2)
        self.assertNotIn("•", main.render(release))


class TestDegradedRun(unittest.TestCase):
    """Post what you can, but never look healthy on partial data."""

    def test_failed_source_is_reported_not_swallowed(self):
        def dead():
            raise RuntimeError("connection reset")

        def alive():
            return [main.Release("ios", "26.6", "stable", title="iOS 26.6")]

        original = (main.from_gdmf, main.from_security_page, main.from_developer_feed)
        main.from_gdmf, main.from_security_page, main.from_developer_feed = (
            dead, alive, alive,
        )
        try:
            releases, failed = main.collect()
        finally:
            (main.from_gdmf, main.from_security_page,
             main.from_developer_feed) = original

        self.assertEqual(len(releases), 1)  # the news still gets through
        self.assertEqual(failed, ["dead"])  # and the failure is visible


class TestFetchIsNeverServedFromACache(unittest.TestCase):
    """Polling every 60s is pointless if the CDN keeps handing back the
    same stale feed. That made beta 6, out at 17:00, a post at 17:57."""

    def setUp(self):
        self.seen = {}
        self.original = main.urllib.request.urlopen

        class Reply:
            def read(self):
                return b"payload"

            def __enter__(self_inner):
                return self_inner

            def __exit__(self, *_):
                return False

        def urlopen(request, timeout=None, context=None):
            self.seen["url"] = request.full_url
            self.seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
            return Reply()

        main.urllib.request.urlopen = urlopen

    def tearDown(self):
        main.urllib.request.urlopen = self.original

    def test_a_parameter_the_cache_has_not_seen_is_added(self):
        main.fetch("https://developer.apple.com/news/releases/rss/releases.rss")
        self.assertRegex(self.seen["url"], r"\?t=\d{10}$")

    def test_a_url_that_already_has_a_query_keeps_it(self):
        main.fetch("https://example.apple.com/thing?lang=en")
        self.assertIn("lang=en", self.seen["url"])
        self.assertRegex(self.seen["url"], r"&t=\d{10}$")

    def test_it_also_asks_the_cache_not_to_answer(self):
        main.fetch("https://support.apple.com/en-us/100100")
        self.assertEqual(self.seen["headers"].get("cache-control"), "no-cache")
        self.assertEqual(self.seen["headers"].get("pragma"), "no-cache")

    def test_two_calls_a_minute_apart_do_not_reuse_a_stamp(self):
        main.fetch("https://example.apple.com/a")
        first = self.seen["url"]
        main.time.sleep(0)  # no wait needed; the stamp is seconds since epoch
        original_time = main.time.time
        main.time.time = lambda: original_time() + 61
        try:
            main.fetch("https://example.apple.com/a")
        finally:
            main.time.time = original_time
        self.assertNotEqual(first, self.seen["url"])


class TestSend(unittest.TestCase):
    """A failed send is not recorded, so it retries — but giving up on the
    first dropped packet makes that retry a minute late for nothing."""

    def setUp(self):
        self.slept = []
        self.original_sleep = main.time.sleep
        self.original_urlopen = main.urllib.request.urlopen
        main.time.sleep = self.slept.append

    def tearDown(self):
        main.time.sleep = self.original_sleep
        main.urllib.request.urlopen = self.original_urlopen

    def replies(self, *outcomes):
        """Each outcome is either bytes to return or an exception to raise."""
        self.calls = 0

        class Reply:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def urlopen(request, timeout=None):
            outcome = outcomes[min(self.calls, len(outcomes) - 1)]
            self.calls += 1
            if isinstance(outcome, Exception):
                raise outcome
            return Reply(outcome)

        main.urllib.request.urlopen = urlopen

    def test_a_dropped_connection_is_retried_not_abandoned(self):
        import urllib.error

        self.replies(urllib.error.URLError("connection reset"), b'{"ok": true}')
        main.send("token", "-100", "iOS 27.0")
        self.assertEqual(self.calls, 2)

    def test_a_reply_that_is_not_json_is_retried(self):
        self.replies(b"<html>502 Bad Gateway</html>", b'{"ok": true}')
        main.send("token", "-100", "iOS 27.0")
        self.assertEqual(self.calls, 2)

    def test_a_rate_limit_waits_exactly_as_long_as_asked(self):
        import urllib.error

        too_fast = urllib.error.HTTPError(
            "url", 429, "Too Many Requests", {},
            io.BytesIO(b'{"ok": false, "parameters": {"retry_after": 7}}'),
        )
        self.replies(too_fast, b'{"ok": true}')
        main.send("token", "-100", "iOS 27.0")
        self.assertIn(8.0, self.slept)  # what Telegram asked, plus a second

    def test_a_real_refusal_is_raised_rather_than_retried_forever(self):
        self.replies(b'{"ok": false, "description": "chat not found"}')
        with self.assertRaises(RuntimeError) as caught:
            main.send("token", "-100", "iOS 27.0")
        self.assertIn("chat not found", str(caught.exception))
        self.assertEqual(self.calls, 1)

    def test_it_gives_up_eventually_instead_of_hanging_the_watch(self):
        import urllib.error

        self.replies(urllib.error.URLError("still down"))
        with self.assertRaises(RuntimeError):
            main.send("token", "-100", "iOS 27.0")
        self.assertEqual(self.calls, 5)


class TestFloodGuard(unittest.TestCase):
    """Every floor rests on dates. If Apple's format changes, every row
    parses as undated, undated counts as current, and the archive posts."""

    def setUp(self):
        import os
        import tempfile

        handle, self.state = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w") as file:
            json.dump({"updated": None, "entries": {}}, file)

        self.original = (
            main.from_gdmf, main.from_security_page, main.from_developer_feed,
        )
        main.from_security_page = lambda: []
        main.from_developer_feed = lambda: []

        self.environ = dict(os.environ)
        os.environ["DRY_RUN"] = "true"
        os.environ["STATE_PATH"] = self.state

    def tearDown(self):
        import os

        (main.from_gdmf, main.from_security_page,
         main.from_developer_feed) = self.original
        os.environ.clear()
        os.environ.update(self.environ)
        Path(self.state).unlink(missing_ok=True)

    def run_with(self, releases):
        import contextlib
        import io

        main.from_gdmf = lambda: list(releases)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main.main()
        return code, out.getvalue()

    def entries(self):
        with open(self.state, encoding="utf-8") as file:
            return json.load(file)["entries"]

    def undated(self, count):
        return [
            main.Release("ios", f"1.{n}", "stable", title=f"iOS 1.{n}", build=f"B{n}")
            for n in range(count)
        ]

    def test_a_believable_day_still_goes_out(self):
        """Apple's busiest day on record is 14 releases. That must post."""
        code, out = self.run_with(self.undated(14))
        self.assertEqual(code, 0)
        self.assertEqual(out.count("would send"), 14)

    def test_a_back_catalogue_is_refused(self):
        code, out = self.run_with(self.undated(300))
        self.assertEqual(code, 1)          # the run goes red
        self.assertNotIn("would send", out)  # and the channel hears nothing

    def test_nothing_is_recorded_when_it_refuses(self):
        """The backlog must still go out once the fault is fixed."""
        self.run_with(self.undated(300))
        self.assertEqual(self.entries(), {})

    def test_an_unreadable_record_stops_the_run_rather_than_reading_as_empty(self):
        """Half a file, or a corrupt one, is not the same as no releases
        announced yet. Believing that would repeat a month of posts."""
        Path(self.state).write_text('{"entries": {"ios|27.0|stab', encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_with([main.Release("ios", "27.0", "stable", title="iOS 27.0")])

    def test_a_missing_record_is_still_a_first_run(self):
        Path(self.state).unlink()
        code, out = self.run_with(
            [main.Release("ios", "27.0", "stable", title="iOS 27.0")]
        )
        self.assertEqual(code, 0)
        self.assertIn("would send", out)


class TestFastRound(unittest.TestCase):
    """Skipping the 1.29 MB index between rounds is a choice, not a fault."""

    def test_the_heavy_source_is_left_alone_and_not_counted_as_failed(self):
        def security():
            raise AssertionError("the security index must not be read")

        def gdmf():
            return [main.Release("ios", "26.6", "stable", title="iOS 26.6")]

        def feed():
            return [main.Release("macos", "26.6", "stable", title="macOS 26.6")]

        original = (main.from_gdmf, main.from_security_page, main.from_developer_feed)
        main.from_gdmf, main.from_security_page, main.from_developer_feed = (
            gdmf, security, feed,
        )
        try:
            releases, failed = main.collect(skip_security_page=True)
            _, failed_on_a_full_round = main.collect()
        finally:
            (main.from_gdmf, main.from_security_page,
             main.from_developer_feed) = original

        self.assertEqual(len(releases), 2)
        self.assertEqual(failed, [])
        # Without the flag it does reach it, and the refusal shows up.
        self.assertEqual(failed_on_a_full_round, ["security"])



if __name__ == "__main__":
    unittest.main()
