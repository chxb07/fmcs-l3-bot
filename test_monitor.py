import os
import json
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import monitor
from monitor import (
    normalize_post_url,
    is_valid_post_link,
    load_seen,
    save_seen,
    load_baseline_state,
    save_baseline_state,
    clean_post_text,
    format_telegram_message,
    process_posts_scan,
    get_send_initial_batch_on_start,
    send_post_to_telegram,
    extract_posts,
    StateFileError,
    FatalStatePersistenceError,
    apply_legacy_seen_migration,
    is_telegram_destination_configured,
    atomic_write_text,
)


class TestNormalization(unittest.TestCase):

    def test_tracking_params_stripped(self):
        url_web = "https://web.facebook.com/fmcs.univbba/posts/pfbid0ybP?__cft__[0]=123&__tn__=456"
        url_plain = "https://facebook.com/fmcs.univbba/posts/pfbid0ybP/"
        url_www = "https://www.facebook.com/fmcs.univbba/posts/pfbid0ybP"
        expected = "https://www.facebook.com/posts/pfbid0ybP"
        self.assertEqual(normalize_post_url(url_web), expected)
        self.assertEqual(normalize_post_url(url_plain), expected)
        self.assertEqual(normalize_post_url(url_www), expected)

    def test_http_https_and_query_order(self):
        a = "http://m.facebook.com/fmcs.univbba/posts/pfbid0ybP?__tn__=R"
        b = "https://web.facebook.com/fmcs.univbba/posts/pfbid0ybP/"
        expected = "https://www.facebook.com/posts/pfbid0ybP"
        self.assertEqual(normalize_post_url(a), expected)
        self.assertEqual(normalize_post_url(b), expected)

    def test_permalink_matches_posts_path_when_story_fbid_equals_post_id(self):
        posts_url = "https://www.facebook.com/fmcs.univbba/posts/12345"
        permalink = "https://m.facebook.com/permalink.php?id=67890&story_fbid=12345&mibextid=xyz"
        story = "http://facebook.com/story.php?story_fbid=12345&id=67890"
        expected = "https://www.facebook.com/posts/12345"
        self.assertEqual(normalize_post_url(posts_url), expected)
        self.assertEqual(normalize_post_url(permalink), expected)
        self.assertEqual(normalize_post_url(story), expected)

    def test_does_not_merge_different_post_ids(self):
        a = normalize_post_url("https://www.facebook.com/fmcs.univbba/posts/12345")
        b = normalize_post_url("https://www.facebook.com/permalink.php?story_fbid=99999&id=67890")
        self.assertNotEqual(a, b)

    def test_does_not_merge_pfbid_with_numeric_id(self):
        a = normalize_post_url("https://www.facebook.com/fmcs.univbba/posts/pfbidABC")
        b = normalize_post_url("https://www.facebook.com/permalink.php?story_fbid=12345&id=1")
        self.assertNotEqual(a, b)

    def test_does_not_merge_photo_or_video_with_post(self):
        post = normalize_post_url("https://www.facebook.com/fmcs.univbba/posts/200")
        photo = normalize_post_url("https://www.facebook.com/fmcs.univbba/photos/a.100/200/")
        video = normalize_post_url("https://www.facebook.com/fmcs.univbba/videos/200/")
        self.assertNotEqual(post, photo)
        self.assertNotEqual(post, video)
        self.assertIn("/photos/", photo)
        self.assertIn("/videos/", video)

    def test_permalink_preserves_non_post_query_when_no_story_fbid(self):
        url = "https://www.facebook.com/photo.php?fbid=111&set=a.222&mibextid=xyz"
        expected = "https://www.facebook.com/photo.php?fbid=111&set=a.222"
        self.assertEqual(normalize_post_url(url), expected)

    def test_load_seen_normalizes_old_urls(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            seen_file = Path(tmpdir) / "seen_posts.json"
            unnormalized_data = [
                "https://web.facebook.com/fmcs.univbba/posts/pfbid0ybP?__cft__[0]=AZiug&__tn__=%2CO%2CP-R",
                "https://www.facebook.com/fmcs.univbba/posts/pfbid0ybP/",
            ]
            seen_file.write_text(json.dumps(unnormalized_data), encoding="utf-8")
            seen_set = load_seen(seen_file)
            self.assertEqual(len(seen_set), 1)
            expected = "https://www.facebook.com/posts/pfbid0ybP"
            self.assertIn(expected, seen_set)
            # After save, disk should have normalized format
            save_seen(seen_set, seen_file)
            on_disk = json.loads(seen_file.read_text(encoding="utf-8"))
            self.assertEqual(on_disk, [expected])

    def test_load_seen_rejects_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            seen_file = Path(tmpdir) / "seen_posts.json"
            for state in ([None], [""], [1]):
                with self.subTest(state=state):
                    seen_file.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(StateFileError):
                        load_seen(seen_file)

    def test_is_valid_post_link(self):
        self.assertTrue(is_valid_post_link("https://www.facebook.com/fmcs.univbba/posts/pfbid0123"))
        self.assertTrue(is_valid_post_link("https://www.facebook.com/fmcs.univbba/photos/a.100/200/"))
        self.assertTrue(is_valid_post_link("https://www.facebook.com/permalink.php?story_fbid=123&id=456"))
        self.assertFalse(is_valid_post_link("https://www.facebook.com/fmcs.univbba/posts"))
        self.assertFalse(is_valid_post_link("https://www.facebook.com/fmcs.univbba"))
        self.assertFalse(is_valid_post_link("https://external-website.com/posts/123"))


class TestBaselineState(unittest.TestCase):

    def test_load_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            baseline_file = Path(tmpdir) / "baseline_state.json"
            state = load_baseline_state(baseline_file)
            self.assertFalse(state["baseline_completed"])
            self.assertFalse(state["first_start_post_sent"])
            self.assertEqual(state["historical_urls"], [])
            self.assertEqual(state["pending_posts"], [])

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            baseline_file = Path(tmpdir) / "baseline_state.json"
            state = {
                "baseline_completed": True,
                "history_migration_completed": True,
                "historical_urls": ["https://example.com/post1"],
                "last_successful_scan_at": "2026-10-08T12:00:00+00:00",
            }
            save_baseline_state(state, baseline_file)
            loaded = load_baseline_state(baseline_file)
            self.assertTrue(loaded["baseline_completed"])
            self.assertTrue(loaded["history_migration_completed"])
            self.assertFalse(loaded["first_start_post_sent"])
            self.assertEqual(loaded["historical_urls"], ["https://example.com/post1"])
            self.assertEqual(loaded["pending_posts"], [])
            self.assertEqual(
                loaded["last_successful_scan_at"],
                "2026-10-08T12:00:00+00:00",
            )

    def test_rejects_invalid_baseline_fields(self):
        invalid_states = [
            {"baseline_completed": "false", "historical_urls": []},
            {"baseline_completed": True, "historical_urls": None},
            {"baseline_completed": True, "historical_urls": [None]},
            {
                "baseline_completed": True,
                "history_migration_completed": "false",
                "historical_urls": [],
            },
            {"historical_urls": []},
            {
                "baseline_completed": True,
                "historical_urls": [],
                "pending_posts": [{"url": "https://facebook.com/posts/1"}],
            },
            {
                "baseline_completed": True,
                "historical_urls": [],
                "first_start_post_sent": "false",
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            baseline_file = Path(tmpdir) / "baseline_state.json"
            for state in invalid_states:
                with self.subTest(state=state):
                    baseline_file.write_text(json.dumps(state), encoding="utf-8")
                    with self.assertRaises(StateFileError):
                        load_baseline_state(baseline_file)


class TestPublicationDateDiagnostics(unittest.TestCase):

    def test_date_attribute_categories(self):
        cases = [
            ("2026-09-18T09:00:00+00:00", "absolute_valid"),
            ("Il y a 2 heures", "relative_text"),
            ("", "empty"),
            ("not a date", "unknown"),
        ]
        for value, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    monitor._classify_date_attribute(value),
                    expected,
                )

    def test_all_publication_date_diagnostic_categories(self):
        cases = [
            (None, 0, "time_element_missing"),
            ("2026-09-18T09:00:00+00:00", 2, "multiple_time_elements"),
            (None, 1, "datetime_missing_or_empty"),
            ("not-a-date", 1, "invalid_iso_format"),
            ("2026-09-18T09:00:00", 1, "timezone_missing"),
            ("2026-09-18T09:00:00+00:00", 1, "valid"),
        ]
        for value, count, expected_category in cases:
            with self.subTest(category=expected_category):
                category, parsed = monitor._diagnose_published_at(value, count)
                self.assertEqual(category, expected_category)
                self.assertEqual(category == "valid", parsed is not None)

    def test_missing_publication_date_does_not_block_startup_send(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file.write_text(json.dumps({
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": False,
                    "historical_urls": [],
                    "pending_posts": [],
                }), encoding="utf-8")
                baseline_state = load_baseline_state(baseline_file)
                seen = {"https://www.facebook.com/posts/pfbidSECRET"}
                posts = [
                    {
                        "url": "https://www.facebook.com/posts/pfbidPRIVATE",
                        "text": "private content",
                        "published_at": None,
                        "_time_element_count": 0,
                    }
                ]
                bot = AsyncMock()
                _, sent = await process_posts_scan(
                    posts=posts,
                    seen=seen,
                    baseline_state=baseline_state,
                    bot=bot,
                    chat_id=123,
                    filepath=seen_file,
                    baseline_filepath=baseline_file,
                    send_first_start_post=True,
                )
                self.assertEqual(sent, 1)
                bot.send_message.assert_awaited_once()
                self.assertIn(
                    "https://www.facebook.com/posts/pfbidPRIVATE",
                    seen,
                )
                self.assertTrue(
                    baseline_state["latest_post_startup_migration_completed"]
                )
        asyncio.run(run())


class TestTextCleaning(unittest.TestCase):

    def test_removes_page_header(self):
        raw = "Faculty of Mathematics and Computer Science جامعة برج بوعريريج\nil y a 22 heures\n22 h\n·\nاعلان عن المنح المؤقت\nToutes les réactions :\n2\nJ'aime\nCommenter..."
        result = clean_post_text(raw)
        self.assertNotIn("Faculty of Mathematics", result)
        self.assertNotIn("il y a 22 heures", result)
        self.assertNotIn("J'aime", result)
        self.assertNotIn("Toutes les réactions", result)
        self.assertIn("اعلان عن المنح المؤقت", result)

    def test_preserves_arabic_french_body(self):
        raw = "Faculty of Mathematics and Computer Science جامعة برج بوعريريج\n3 j\n·\nستجدون في الرابط أدناه التوزيعات\nDRIVE.GOOGLE.COM\nemploi - Google Drive\nToutes les réactions :\n15\nJ'aime"
        result = clean_post_text(raw)
        self.assertIn("ستجدون في الرابط أدناه التوزيعات", result)
        self.assertIn("DRIVE.GOOGLE.COM", result)
        self.assertNotIn("J'aime", result)

    def test_empty_input(self):
        self.assertEqual(clean_post_text(""), "")
        self.assertEqual(clean_post_text("   "), "")

    def test_strips_relative_timestamps(self):
        raw = "Faculty of Mathematics and Computer Science جامعة برج بوعريريج\nil y a 23 heures\n23 h\n·\nإعلان\nToutes les réactions :\n11"
        result = clean_post_text(raw)
        self.assertNotIn("il y a 23 heures", result)
        self.assertNotIn("23 h", result)
        self.assertIn("إعلان", result)


class TestMessageFormatting(unittest.TestCase):

    def test_format_message_max_length(self):
        url = "https://www.facebook.com/fmcs.univbba/posts/pfbid0123"
        long_text = "أ" * 5000
        msg = format_telegram_message(long_text, url)
        self.assertLessEqual(len(msg), 4096)
        self.assertTrue(msg.endswith(f"\n\n🔗 {url}"))

    def test_format_message_with_timestamp(self):
        url = "https://www.facebook.com/fmcs.univbba/posts/pfbid0123"
        msg = format_telegram_message("إعلان", url, timestamp="22 h")
        self.assertIn("(22 h)", msg)
        self.assertIn("إعلان", msg)
        self.assertIn(url, msg)

    def test_format_message_does_not_include_image_url(self):
        url = "https://www.facebook.com/fmcs.univbba/posts/pfbid0123"
        img = "https://scontent.fbcdn.net/photo.jpg"
        msg = format_telegram_message("إعلان", url)
        self.assertNotIn(img, msg)
        self.assertIn("🔗", msg)
        self.assertIn(url, msg)

    def test_format_message_respects_max_length(self):
        url = "https://www.facebook.com/fmcs.univbba/posts/pfbid0123"
        msg = format_telegram_message("أ" * 5000, url, max_length=1024)
        self.assertLessEqual(len(msg), 1024)


class TestEnvConfig(unittest.TestCase):

    def test_get_send_initial_batch_true(self):
        with patch.dict(os.environ, {"SEND_INITIAL_BATCH_ON_START": "true"}):
            self.assertTrue(get_send_initial_batch_on_start())

    def test_get_send_initial_batch_false_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(get_send_initial_batch_on_start())

    def test_legacy_send_latest_on_start_still_works(self):
        with patch.dict(os.environ, {"SEND_LATEST_ON_START": "true"}, clear=True):
            self.assertTrue(get_send_initial_batch_on_start())


class TestScanLogic(unittest.TestCase):

    def _make_posts(self, ids):
        newest = datetime.now(timezone.utc) + timedelta(days=2)
        return [
            {
                "url": f"https://www.facebook.com/fmcs.univbba/posts/pfbid{i}",
                "text": f"Post {i}",
                "timestamp": "1 j",
                "image_url": None,
                "published_at": (
                    newest - timedelta(days=index)
                ).isoformat(),
            }
            for index, i in enumerate(ids)
        ]

    def _existing_seen_urls(self):
        return {
            "https://www.facebook.com/posts/pfbid02NvQANXvV4GkJN84oog62q3cN6gC6g9FugeFqLdNr3hogYS4k6ocMmcSXshXxGEtVl",
            "https://www.facebook.com/posts/pfbid02XgfSWWpVrCUy2dMCAD6q9Z78h1wNqffN2F2ksAVrc5hAYUN8EP3GajTGn21w4JPHl",
            "https://www.facebook.com/posts/pfbid02ctr8jJAteepEtLnfGdm78AaMnxdZnfuxuwtYw9tBcbb4qZ42Guof4Q585euY3d5vl",
            "https://www.facebook.com/posts/pfbid02innVj5tMDE9MRFMvka2HJ8jr8MwwxPd9BZP8zdr23eWQFXmGMgZdme3awLVNr1hrl",
            "https://www.facebook.com/posts/pfbid035ughpyMTy7LcToTNyTfVxiWcZetDxWG5dCyHMThn8sbNifRhBGUU9wb7mh8z3CVWl",
            "https://www.facebook.com/posts/pfbid09AHxsRaa8CEwRiyVuguafhhgkscH3pmYJKL1YYPCnsQ5Q4x37JZyb7WGSY5vnAPyl",
            "https://www.facebook.com/posts/pfbid0hYvfJxAon3KqdaYAZpVNefXUG8ZdWxFT4CYFVdXi3QShU3MDYNPWxqTEvsMsFv3jl",
            "https://www.facebook.com/posts/pfbid0pboq6WFukj5LgveoFRkPLjDbYhZtaLYy7CyVUpLVa77dasdRVuFfYAXKdq5iBCLpl",
            "https://www.facebook.com/posts/pfbid0ybPvogWdV91vfmCMGc12xeZuyRoxVCk1GrLpEydcTPuq7Vejpy1CY3VAjDyHewwCl",
        }

    def _make_posts_with_images(self, ids):
        posts = self._make_posts(ids)
        for post, post_id in zip(posts, ids):
            post["image_url"] = f"https://scontent.fbcdn.net/{post_id}.jpg"
        return posts

    def test_existing_production_state_migrates_visible_unseen_posts_as_historical(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                existing_seen = self._existing_seen_urls()
                original_seen = set(existing_seen)
                save_seen(existing_seen, seen_file)
                baseline_file.write_text(json.dumps({
                    "baseline_completed": True,
                    "historical_urls": [],
                }), encoding="utf-8")
                baseline_state = load_baseline_state(baseline_file)
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                visible_post = self._make_posts(["OLD_UNRECORDED"])[0]

                detected, sent = await process_posts_scan(
                    posts=[visible_post], seen=existing_seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file,
                )

                self.assertEqual(detected, 1)
                self.assertEqual(sent, 0)
                self.assertEqual(existing_seen, original_seen)
                self.assertEqual(len(load_seen(seen_file)), 9)
                self.assertIn(visible_post["url"].replace(
                    "/fmcs.univbba", ""
                ), baseline_state["historical_urls"])
                self.assertTrue(baseline_state["history_migration_completed"])
                self.assertEqual(mock_bot.send_message.call_count, 1)  # Migration notice only
                self.assertNotIn(
                    "OLD_UNRECORDED",
                    mock_bot.send_message.call_args.kwargs["text"],
                )
        asyncio.run(run())

    def test_historical_migration_survives_restart_without_reclassifying_posts(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen = self._existing_seen_urls()
                baseline_file.write_text(json.dumps({
                    "baseline_completed": True,
                    "historical_urls": [],
                }), encoding="utf-8")
                first_state = load_baseline_state(baseline_file)
                post = self._make_posts(["OLD_UNRECORDED"])[0]
                first_bot = AsyncMock()
                first_bot.send_message.return_value = True

                await process_posts_scan(
                    posts=[post], seen=seen, baseline_state=first_state,
                    bot=first_bot, chat_id=123,
                    filepath=Path(tmpdir) / "seen_posts.json",
                    baseline_filepath=baseline_file,
                )

                restored_state = load_baseline_state(baseline_file)
                restart_bot = AsyncMock()
                restart_bot.send_message.return_value = True
                _, sent = await process_posts_scan(
                    posts=[post], seen=seen, baseline_state=restored_state,
                    bot=restart_bot, chat_id=123,
                    filepath=Path(tmpdir) / "seen_posts.json",
                    baseline_filepath=baseline_file,
                )

                self.assertEqual(sent, 0)
                restart_bot.send_message.assert_not_called()
                self.assertTrue(restored_state["history_migration_completed"])
        asyncio.run(run())

    def test_migration_preserves_and_retries_pending_delivery_after_restart(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen_file = Path(tmpdir) / "seen_posts.json"
                seen = self._existing_seen_urls()
                pending_post = self._make_posts(["PENDING"])[0]
                pending_post["confirmed_new_post"] = True
                old_post = self._make_posts(["OLD_UNRECORDED"])[0]
                baseline_file.write_text(json.dumps({
                    "baseline_completed": True,
                    "first_start_post_sent": True,
                    "last_successful_scan_at": "2026-09-23T12:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [pending_post],
                }), encoding="utf-8")
                state = load_baseline_state(baseline_file)
                migration_bot = AsyncMock()
                migration_bot.send_message.return_value = True

                restart_bot = AsyncMock()
                restart_bot.send_message.return_value = True
                with (
                    patch(
                        "monitor.send_post_to_telegram",
                        new_callable=AsyncMock,
                        side_effect=[False, True],
                    ) as send_mock,
                    patch("monitor.asyncio.sleep", new_callable=AsyncMock),
                ):
                    _, first_sent = await process_posts_scan(
                        posts=[old_post, pending_post], seen=seen, baseline_state=state,
                        bot=migration_bot, chat_id=123, filepath=seen_file,
                        baseline_filepath=baseline_file,
                    )
                    self.assertEqual(first_sent, 0)
                    self.assertEqual(len(state["pending_posts"]), 1)

                    restored_state = load_baseline_state(baseline_file)
                    self.assertTrue(restored_state["first_start_post_sent"])
                    _, sent = await process_posts_scan(
                        posts=[], seen=seen, baseline_state=restored_state,
                        bot=restart_bot, chat_id=123, filepath=seen_file,
                        baseline_filepath=baseline_file,
                    )

                self.assertEqual(sent, 1)
                self.assertEqual(send_mock.await_count, 2)
                self.assertEqual(restored_state["pending_posts"], [])
                self.assertIn(pending_post["url"].replace(
                    "/fmcs.univbba", ""
                ), load_seen(seen_file))
        asyncio.run(run())

    def test_new_posts_after_migration_are_sent_oldest_first(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen_file = Path(tmpdir) / "seen_posts.json"
                seen = self._existing_seen_urls()
                baseline_file.write_text(json.dumps({
                    "baseline_completed": True,
                    "historical_urls": [],
                }), encoding="utf-8")
                state = load_baseline_state(baseline_file)
                old_post = self._make_posts(["OLD"])[0]
                old_post["text"] = "HISTORICAL_ONLY_CONTENT"
                migration_bot = AsyncMock()
                migration_bot.send_message.return_value = True
                await process_posts_scan(
                    posts=[old_post], seen=seen, baseline_state=state,
                    bot=migration_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file,
                )

                restart_state = load_baseline_state(baseline_file)
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                new_posts = self._make_posts(["NEWEST", "OLDER"])
                _, sent = await process_posts_scan(
                    posts=[new_posts[0], new_posts[1], old_post],
                    seen=seen, baseline_state=restart_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file,
                )

                self.assertEqual(sent, 2)
                calls = mock_bot.send_message.call_args_list
                self.assertIn("OLDER", calls[0].kwargs["text"])
                self.assertIn("NEWEST", calls[1].kwargs["text"])
                self.assertNotIn("HISTORICAL_ONLY_CONTENT", calls[0].kwargs["text"])
                self.assertNotIn("HISTORICAL_ONLY_CONTENT", calls[1].kwargs["text"])
        asyncio.run(run())

    def test_baseline_without_batch_marks_all_historical(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                posts = self._make_posts(["001", "002"])
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                detected, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(detected, 2)
                self.assertEqual(sent, 0)
                self.assertEqual(len(seen), 0)  # nothing delivered
                self.assertTrue(baseline_state["baseline_completed"])
                self.assertEqual(len(baseline_state["historical_urls"]), 2)
                # Only summary message should have been sent
                mock_bot.send_message.assert_called_once()
        asyncio.run(run())

    def test_unchanged_scan_sends_nothing(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                posts = self._make_posts(["001", "002"])
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                # First scan - baseline without batch
                await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                mock_bot.reset_mock()
                # Second scan - no changes
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 0)
                mock_bot.send_message.assert_not_called()
        asyncio.run(run())

    def test_new_post_sent_once_and_saved(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                posts = self._make_posts(["001", "002"])
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                # Baseline
                await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                mock_bot.reset_mock()
                # New post arrives
                posts_new = self._make_posts(["001", "002", "003"])
                _, sent = await process_posts_scan(
                    posts=posts_new, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 1)
                self.assertEqual(len(seen), 1)
                mock_bot.send_message.assert_called_once()
                self.assertIn("pfbid003", mock_bot.send_message.call_args.kwargs["text"])
        asyncio.run(run())

    def test_multiple_new_posts_sent_oldest_first(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                # Pre-populate seen with one old post
                seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid000"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                # Two new posts appear: newest first (index 0 = newest)
                posts = self._make_posts(["NEW_A", "NEW_B", "000"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 2)
                # reversed(new_posts) means oldest first: NEW_B sent before NEW_A
                calls = mock_bot.send_message.call_args_list
                self.assertEqual(len(calls), 2)
                self.assertIn("pfbidNEW_B", calls[0].kwargs["text"])
                self.assertIn("pfbidNEW_A", calls[1].kwargs["text"])
        asyncio.run(run())

    def test_failed_send_does_not_mark_post_as_seen(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.side_effect = Exception("Network error")
                seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid000"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                posts = self._make_posts(["000", "NEW"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 0)
                self.assertNotIn("https://www.facebook.com/fmcs.univbba/posts/pfbidNEW", seen)
        asyncio.run(run())

    def test_initial_batch_sends_only_the_latest_post(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                posts = self._make_posts(["001", "002", "003", "004", "005"])
                detected, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                self.assertEqual(detected, 5)
                self.assertEqual(sent, 1)
                # One post message + one summary
                self.assertEqual(mock_bot.send_message.call_count, 2)
                self.assertIn("pfbid001", mock_bot.send_message.call_args_list[0].kwargs["text"])
                self.assertEqual(len(seen), 1)
                self.assertTrue(baseline_state["baseline_completed"])
                self.assertEqual(len(baseline_state["historical_urls"]), 4)
        asyncio.run(run())

    def test_initial_batch_uses_first_feed_post_without_requiring_timestamp(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                # This code path uses the page's newest-first feed order.
                posts = self._make_posts(["NEWEST", "MIDDLE", "OLDEST"])
                posts[0]["published_at"] = "2026-09-12T10:00:00+00:00"
                posts[1]["published_at"] = "2026-09-18T10:00:00+00:00"
                posts[2]["published_at"] = "2026-09-17T10:00:00+00:00"
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                self.assertEqual(sent, 1)
                calls = mock_bot.send_message.call_args_list
                self.assertIn("pfbidNEWEST", calls[0].kwargs["text"])
                self.assertEqual(len(seen), 1)
        asyncio.run(run())

    def test_initial_batch_sends_first_post_when_publication_date_is_unavailable(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                posts = self._make_posts(["A", "B"])
                posts[1]["published_at"] = None
                seen = set()
                baseline_state = load_baseline_state(Path(tmpdir) / "baseline.json")
                _, sent = await process_posts_scan(
                    posts=posts,
                    seen=seen,
                    baseline_state=baseline_state,
                    bot=mock_bot,
                    chat_id=123,
                    filepath=Path(tmpdir) / "seen.json",
                    baseline_filepath=Path(tmpdir) / "baseline.json",
                    send_initial_batch_on_start=True,
                )
                self.assertEqual(sent, 1)
                self.assertTrue(baseline_state["baseline_completed"])
                self.assertIn("https://www.facebook.com/posts/pfbidA", seen)
                self.assertEqual(
                    baseline_state["historical_urls"],
                    ["https://www.facebook.com/posts/pfbidB"],
                )
        asyncio.run(run())

    def test_start_sends_latest_dom_post_even_when_already_sent(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                posts = self._make_posts([
                    "LATEST", "OLDER_1", "OLDER_2", "OLDER_3", "OLDER_4",
                    "OLDER_5", "OLDER_6", "OLDER_7", "OLDER_8",
                ])
                seen = {normalize_post_url(posts[0]["url"])}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": False,
                    "historical_urls": [],
                    "pending_posts": [],
                }
                bot = AsyncMock()
                bot.send_message.return_value = True

                with patch("monitor.asyncio.sleep", new_callable=AsyncMock):
                    _, sent = await process_posts_scan(
                        posts=posts,
                        seen=seen,
                        bot=bot,
                        chat_id=123,
                        filepath=seen_file,
                        baseline_filepath=baseline_file,
                        baseline_state=baseline_state,
                        send_first_start_post=True,
                    )

                self.assertEqual(sent, 1)
                bot.send_message.assert_awaited_once()
                self.assertIn(
                    "pfbidLATEST",
                    bot.send_message.call_args.kwargs["text"],
                )
                self.assertTrue(baseline_state["first_start_post_sent"])
                self.assertEqual(seen, {normalize_post_url(posts[0]["url"])})
                self.assertEqual(
                    set(baseline_state["historical_urls"]),
                    {
                        normalize_post_url(post["url"])
                        for post in posts
                    },
                )
                self.assertEqual(baseline_state["pending_posts"], [])

                bot.reset_mock()
                restored_state = load_baseline_state(baseline_file)
                _, repeated_sent = await process_posts_scan(
                    posts=posts,
                    seen=seen,
                    bot=bot,
                    chat_id=123,
                    filepath=seen_file,
                    baseline_filepath=baseline_file,
                    baseline_state=restored_state,
                    send_first_start_post=True,
                )
                self.assertEqual(repeated_sent, 1)
                bot.send_message.assert_awaited_once()
                self.assertIn(
                    "pfbidLATEST",
                    bot.send_message.call_args.kwargs["text"],
                )
                self.assertTrue(restored_state["first_start_post_sent"])
        asyncio.run(run())

    def test_first_start_failed_delivery_remains_pending_for_retry(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                post = self._make_posts(["LATEST"])[0]
                seen = {normalize_post_url(post["url"])}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": False,
                    "historical_urls": [],
                    "pending_posts": [],
                }

                with (
                    patch(
                        "monitor.send_post_to_telegram",
                        new_callable=AsyncMock,
                        side_effect=[False, True],
                    ) as send_mock,
                    patch("monitor.asyncio.sleep", new_callable=AsyncMock),
                ):
                    _, first_sent = await process_posts_scan(
                        posts=[post],
                        seen=seen,
                        bot=AsyncMock(),
                        chat_id=123,
                        filepath=seen_file,
                        baseline_filepath=baseline_file,
                        baseline_state=baseline_state,
                        send_first_start_post=True,
                    )
                    self.assertEqual(first_sent, 0)
                    self.assertFalse(baseline_state["first_start_post_sent"])
                    self.assertEqual(
                        len(baseline_state["pending_posts"]),
                        1,
                    )
                    self.assertTrue(
                        baseline_state["pending_posts"][0]["first_start_delivery"]
                    )
                    self.assertIn(normalize_post_url(post["url"]), seen)

                    restored_state = load_baseline_state(baseline_file)
                    _, retry_sent = await process_posts_scan(
                        posts=[],
                        seen=seen,
                        bot=AsyncMock(),
                        chat_id=123,
                        filepath=seen_file,
                        baseline_filepath=baseline_file,
                        baseline_state=restored_state,
                    )

                self.assertEqual(retry_sent, 1)
                self.assertEqual(send_mock.await_count, 2)
                self.assertTrue(restored_state["first_start_post_sent"])
                self.assertEqual(restored_state["pending_posts"], [])
        asyncio.run(run())

    def test_periodic_scan_does_not_send_late_discovered_september_22_post(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": True,
                    "last_successful_scan_at": "2026-09-23T12:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                post = self._make_posts(["SEPTEMBER_22"])[0]
                post["published_at"] = "2026-09-22T10:00:00+00:00"
                bot = AsyncMock()

                with self.assertLogs("root", level="INFO") as captured:
                    _, sent = await process_posts_scan(
                        posts=[post],
                        seen=set(),
                        bot=bot,
                        chat_id=123,
                        filepath=Path(tmpdir) / "seen.json",
                        baseline_filepath=Path(tmpdir) / "baseline.json",
                        baseline_state=baseline_state,
                    )

                self.assertEqual(sent, 0)
                bot.send_message.assert_not_awaited()
                self.assertEqual(baseline_state["pending_posts"], [])
                self.assertTrue(any(
                    "reason=not_newer_than_last_successful_scan"
                    in line
                    for line in captured.output
                ))
        asyncio.run(run())

    def test_missing_scan_checkpoint_safely_seeds_without_sending_visible_posts(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": True,
                    "historical_urls": [],
                    "pending_posts": [],
                }
                post = self._make_posts(["SEPTEMBER_22"])[0]
                post["published_at"] = "2026-09-22T10:00:00+00:00"
                bot = AsyncMock()

                _, sent = await process_posts_scan(
                    posts=[post],
                    seen=set(),
                    bot=bot,
                    chat_id=123,
                    filepath=Path(tmpdir) / "seen.json",
                    baseline_filepath=Path(tmpdir) / "baseline.json",
                    baseline_state=baseline_state,
                )

                self.assertEqual(sent, 0)
                bot.send_message.assert_not_awaited()
                self.assertEqual(baseline_state["pending_posts"], [])
                self.assertIsNotNone(baseline_state["last_successful_scan_at"])
                self.assertEqual(
                    load_baseline_state(
                        Path(tmpdir) / "baseline.json"
                    )["last_successful_scan_at"],
                    baseline_state["last_successful_scan_at"],
                )
        asyncio.run(run())

    def test_periodic_scan_sends_only_confirmed_posts_oldest_first(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": True,
                    "last_successful_scan_at": "2026-09-23T12:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                posts = self._make_posts(["SEPT26", "SEPT24", "SEPT25"])
                for post, published_at in zip(
                    posts,
                    [
                        "2026-09-26T08:00:00+00:00",
                        "2026-09-24T08:00:00+00:00",
                        "2026-09-25T08:00:00+00:00",
                    ],
                ):
                    post["published_at"] = published_at
                bot = AsyncMock()
                bot.send_message.return_value = True

                with patch("monitor.asyncio.sleep", new_callable=AsyncMock):
                    _, sent = await process_posts_scan(
                        posts=posts,
                        seen=set(),
                        bot=bot,
                        chat_id=123,
                        filepath=Path(tmpdir) / "seen.json",
                        baseline_filepath=Path(tmpdir) / "baseline.json",
                        baseline_state=baseline_state,
                    )

                self.assertEqual(sent, 3)
                messages = [
                    call.kwargs["text"]
                    for call in bot.send_message.call_args_list
                ]
                self.assertIn("SEPT24", messages[0])
                self.assertIn("SEPT25", messages[1])
                self.assertIn("SEPT26", messages[2])
                self.assertTrue(all(
                    post["confirmed_new_post"]
                    for post in baseline_state["pending_posts"]
                ) if baseline_state["pending_posts"] else True)
        asyncio.run(run())

    def test_periodic_scan_skips_posts_without_trusted_publication_time(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": True,
                    "last_successful_scan_at": "2026-09-23T12:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                post = self._make_posts(["UNKNOWN_DATE"])[0]
                post["published_at"] = None
                post["_time_element_count"] = 0
                bot = AsyncMock()

                with self.assertLogs("root", level="INFO") as captured:
                    _, sent = await process_posts_scan(
                        posts=[post],
                        seen=set(),
                        bot=bot,
                        chat_id=123,
                        filepath=Path(tmpdir) / "seen.json",
                        baseline_filepath=Path(tmpdir) / "baseline.json",
                        baseline_state=baseline_state,
                    )

                self.assertEqual(sent, 0)
                bot.send_message.assert_not_awaited()
                self.assertEqual(baseline_state["pending_posts"], [])
                self.assertTrue(any(
                    "reason=untrusted_publication_timestamp"
                    in line
                    for line in captured.output
                ))
        asyncio.run(run())

    def test_scan_checkpoint_and_new_pending_state_stay_unchanged_if_save_fails(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                previous_checkpoint = "2026-09-23T12:00:00+00:00"
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "first_start_post_sent": True,
                    "last_successful_scan_at": previous_checkpoint,
                    "historical_urls": [],
                    "pending_posts": [],
                }
                post = self._make_posts(["SAVE_FAILURE"])[0]
                bot = AsyncMock()

                with (
                    patch(
                        "monitor.save_baseline_state",
                        side_effect=OSError("disk full"),
                    ),
                    patch("monitor.asyncio.sleep", new_callable=AsyncMock),
                ):
                    with self.assertRaisesRegex(
                        FatalStatePersistenceError,
                        "Unable to persist scan checkpoint",
                    ):
                        await process_posts_scan(
                            posts=[post],
                            seen=set(),
                            bot=bot,
                            chat_id=123,
                            filepath=Path(tmpdir) / "seen.json",
                            baseline_filepath=Path(tmpdir) / "baseline.json",
                            baseline_state=baseline_state,
                        )

                self.assertEqual(
                    baseline_state["last_successful_scan_at"],
                    previous_checkpoint,
                )
                self.assertEqual(baseline_state["pending_posts"], [])
                bot.send_message.assert_not_awaited()
        asyncio.run(run())

    def test_deferred_startup_migration_still_retries_pending_without_checkpoint_advance(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                previous_checkpoint = "2026-09-23T12:00:00+00:00"
                pending_post = self._make_posts(["PENDING_DURING_MIGRATION"])[0]
                pending_post["confirmed_new_post"] = True
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": False,
                    "first_start_post_sent": True,
                    "last_successful_scan_at": previous_checkpoint,
                    "historical_urls": [],
                    "pending_posts": [pending_post],
                }
                posts = [{
                    "url": "",
                    "text": "No usable URL",
                    "image_url": None,
                    "timestamp": "N/A",
                }]

                with patch(
                    "monitor.send_post_to_telegram",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as send_mock:
                    _, sent = await process_posts_scan(
                        posts=posts,
                        seen={"https://www.facebook.com/posts/recorded"},
                        bot=AsyncMock(),
                        chat_id=123,
                        filepath=Path(tmpdir) / "seen.json",
                        baseline_filepath=Path(tmpdir) / "baseline.json",
                        baseline_state=baseline_state,
                    )

                self.assertEqual(sent, 1)
                send_mock.assert_awaited_once()
                self.assertEqual(baseline_state["pending_posts"], [])
                self.assertEqual(
                    baseline_state["last_successful_scan_at"],
                    previous_checkpoint,
                )
        asyncio.run(run())

    def _legacy_completed_baseline_file(self, path):
        path.write_text(json.dumps({
            "baseline_completed": True,
            "history_migration_completed": True,
            "historical_urls": [],
            "pending_posts": [],
        }), encoding="utf-8")

    def test_legacy_completed_state_migrates_and_sends_only_latest_once(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen_file = Path(tmpdir) / "seen_posts.json"
                self._legacy_completed_baseline_file(baseline_file)
                seen = {"https://www.facebook.com/posts/already-recorded"}
                save_seen(seen, seen_file)
                baseline_state = load_baseline_state(baseline_file)
                posts = self._make_posts(["SEPT18", "SEPT12", "SEPT17"])
                for post in posts:
                    post["published_at"] = None
                bot = AsyncMock()
                bot.send_message.return_value = True

                _, sent = await process_posts_scan(
                    posts=posts,
                    seen=seen,
                    baseline_state=baseline_state,
                    bot=bot,
                    chat_id=123,
                    filepath=seen_file,
                    baseline_filepath=baseline_file,
                    send_first_start_post=True,
                )

                self.assertEqual(sent, 1)
                self.assertEqual(bot.send_message.await_count, 1)
                self.assertIn(
                    "pfbidSEPT18",
                    bot.send_message.call_args.kwargs["text"],
                )
                self.assertTrue(
                    baseline_state["latest_post_startup_migration_completed"]
                )
                self.assertEqual(
                    set(baseline_state["historical_urls"]),
                    {
                        "https://www.facebook.com/posts/pfbidSEPT12",
                        "https://www.facebook.com/posts/pfbidSEPT17",
                        "https://www.facebook.com/posts/pfbidSEPT18",
                    },
                )
                self.assertIn(
                    "https://www.facebook.com/posts/pfbidSEPT18",
                    seen,
                )

                bot.reset_mock()
                _, sent_again = await process_posts_scan(
                    posts=posts,
                    seen=seen,
                    baseline_state=load_baseline_state(baseline_file),
                    bot=bot,
                    chat_id=123,
                    filepath=seen_file,
                    baseline_filepath=baseline_file,
                )
                self.assertEqual(sent_again, 0)
                bot.send_message.assert_not_awaited()
        asyncio.run(run())

    def test_legacy_completed_state_retries_pending_when_latest_already_seen(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen_file = Path(tmpdir) / "seen_posts.json"
                self._legacy_completed_baseline_file(baseline_file)
                posts = self._make_posts(["LATEST", "OLDER"])
                posts[0]["published_at"] = "2026-09-17T09:00:00+00:00"
                posts[1]["published_at"] = "2026-09-18T09:00:00+00:00"
                seen = {
                    "https://www.facebook.com/posts/pfbidALREADY",
                    "https://www.facebook.com/posts/pfbidLATEST",
                }
                pending_post = {
                    "url": "https://www.facebook.com/posts/pfbidPENDING",
                    "text": "Keep pending",
                    "image_url": None,
                    "timestamp": "N/A",
                    "confirmed_new_post": True,
                    "published_at": "2026-09-18T08:00:00+00:00",
                }
                baseline_state = load_baseline_state(baseline_file)
                baseline_state["pending_posts"] = [pending_post]
                bot = AsyncMock()

                with patch(
                    "monitor.send_post_to_telegram",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as send_mock:
                    _, sent = await process_posts_scan(
                        posts=posts,
                        seen=seen,
                        baseline_state=baseline_state,
                        bot=bot,
                        chat_id=123,
                        filepath=seen_file,
                        baseline_filepath=baseline_file,
                    )

                self.assertEqual(sent, 1)
                send_mock.assert_awaited_once_with(bot, 123, pending_post)
                self.assertIn(
                    normalize_post_url(pending_post["url"]),
                    seen,
                )
                self.assertEqual(baseline_state["pending_posts"], [])
                self.assertTrue(
                    load_baseline_state(baseline_file)[
                        "latest_post_startup_migration_completed"
                    ]
                )
                self.assertEqual(load_baseline_state(baseline_file)["pending_posts"], [])
        asyncio.run(run())

    def test_legacy_startup_migration_uses_first_feed_post_without_date(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                baseline_file = Path(tmpdir) / "baseline_state.json"
                seen_file = Path(tmpdir) / "seen_posts.json"
                self._legacy_completed_baseline_file(baseline_file)
                baseline_state = load_baseline_state(baseline_file)
                seen = {"https://www.facebook.com/posts/pfbidRECORDED"}
                posts = self._make_posts(["FIRST", "OLDER"])
                for post in posts:
                    post["published_at"] = None
                bot = AsyncMock()
                bot.send_message.return_value = True

                _, sent = await process_posts_scan(
                    posts=posts,
                    seen=seen,
                    baseline_state=baseline_state,
                    bot=bot,
                    chat_id=123,
                    filepath=seen_file,
                    baseline_filepath=baseline_file,
                    send_first_start_post=True,
                )

                self.assertEqual(sent, 1)
                self.assertTrue(
                    baseline_state["latest_post_startup_migration_completed"]
                )
                self.assertEqual(
                    baseline_state["historical_urls"],
                    [
                        "https://www.facebook.com/posts/pfbidOLDER",
                        "https://www.facebook.com/posts/pfbidFIRST",
                    ],
                )
                self.assertIn("pfbidFIRST", bot.send_message.call_args.kwargs["text"])
        asyncio.run(run())

    def test_subsequent_scan_sends_all_new_posts_even_more_than_3(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid000"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                posts = self._make_posts(["001", "002", "003", "004", "005", "000"])
                detected, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(detected, 6)
                self.assertEqual(sent, 5)
                self.assertEqual(len(seen), 6)
        asyncio.run(run())

    def test_subsequent_posts_are_ordered_oldest_to_newest(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid000"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                # Scraped page order is newest first; reverse it for delivery.
                posts = self._make_posts(["NEW_A", "NEW_B", "NEW_C", "000"])
                posts[0]["published_at"] = "2026-09-10T10:00:00+00:00"
                posts[1]["published_at"] = "2026-09-30T10:00:00+00:00"
                posts[2]["published_at"] = "2026-09-20T10:00:00+00:00"
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 3)
                calls = mock_bot.send_message.call_args_list
                # Publication timestamps, not page order, determine delivery order.
                self.assertIn("pfbidNEW_A", calls[0].kwargs["text"])
                self.assertIn("pfbidNEW_C", calls[1].kwargs["text"])
                self.assertIn("pfbidNEW_B", calls[2].kwargs["text"])
        asyncio.run(run())

    def test_previously_delivered_posts_not_resent(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                posts = self._make_posts(["001", "002"])
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                # First scan - baseline with batch
                await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                mock_bot.reset_mock()
                # Same posts again - nothing new
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 0)
                mock_bot.send_message.assert_not_called()
        asyncio.run(run())

    def test_failed_delivery_retried_without_losing_posts(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.side_effect = Exception("Network error")
                seen = {"https://www.facebook.com/posts/pfbid000"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                posts = self._make_posts(["000", "NEW"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 0)
                self.assertNotIn("https://www.facebook.com/posts/pfbidNEW", seen)
                # Reload persisted state and retry after the post leaves the scan results.
                mock_bot.send_message.side_effect = None
                mock_bot.send_message.return_value = True
                restored_state = load_baseline_state(baseline_file)
                _, sent_retry = await process_posts_scan(
                    posts=[], seen=seen, baseline_state=restored_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent_retry, 1)
                self.assertIn("https://www.facebook.com/posts/pfbidNEW", seen)
                self.assertEqual(restored_state["pending_posts"], [])
        asyncio.run(run())

    def test_seen_state_save_failure_is_fatal_and_keeps_delivery_pending(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = set()
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                post = self._make_posts(["NEW"])[0]

                with patch(
                    "monitor.save_seen",
                    side_effect=OSError("disk full"),
                ) as save_seen_mock:
                    with self.assertRaisesRegex(
                        FatalStatePersistenceError,
                        "Unable to persist delivered post state",
                    ):
                        await process_posts_scan(
                            posts=[post],
                            seen=seen,
                            bot=mock_bot,
                            chat_id=123,
                            filepath=seen_file,
                            baseline_filepath=baseline_file,
                            baseline_state=baseline_state,
                        )

                save_seen_mock.assert_called_once()
                mock_bot.send_message.assert_awaited_once()
                self.assertNotIn(post["url"], seen)
                self.assertEqual(
                    [item["url"] for item in baseline_state["pending_posts"]],
                    [normalize_post_url(post["url"])],
                )
                self.assertEqual(
                    [
                        item["url"]
                        for item in load_baseline_state(baseline_file)["pending_posts"]
                    ],
                    [normalize_post_url(post["url"])],
                )
        asyncio.run(run())

    def test_restart_does_not_retrigger_initial_batch(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                # Simulate existing state (bot restart)
                seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid001"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                posts = self._make_posts(["001", "002", "003"])
                # Even with send_initial_batch_on_start=True, baseline is completed
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                # Should send new posts as regular posts, not as initial batch
                self.assertEqual(sent, 2)
                calls = mock_bot.send_message.call_args_list
                # Both new posts sent individually (oldest first)
                self.assertEqual(len(calls), 2)
                self.assertIn("pfbid003", calls[0].kwargs["text"])
                self.assertIn("pfbid002", calls[1].kwargs["text"])
        asyncio.run(run())

    def test_initial_batch_not_repeated_on_second_scan(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                posts = self._make_posts(["001", "002"])
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                # First scan with batch enabled
                await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                mock_bot.reset_mock()
                # Second scan - batch disabled
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 0)
                mock_bot.send_message.assert_not_called()
        asyncio.run(run())

    def test_initial_batch_failed_posts_not_marked_seen(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.side_effect = Exception("Network error")
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                posts = self._make_posts(["001", "002", "003"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                self.assertEqual(sent, 0)
                # Failed batch posts should NOT be in seen
                self.assertEqual(len(seen), 0)
                # Baseline completes with the selected post pending for retry.
                self.assertTrue(baseline_state["baseline_completed"])
                self.assertEqual(len(baseline_state["historical_urls"]), 2)
                self.assertEqual(len(baseline_state["pending_posts"]), 1)
        asyncio.run(run())

    def test_historical_posts_never_sent_on_later_scan(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                posts = self._make_posts(["001", "002", "003", "004", "005"])
                # One newest post is sent; all other baseline posts are historical.
                await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                mock_bot.reset_mock()
                # Later scan sees the same posts again
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 0)
                mock_bot.send_message.assert_not_called()
        asyncio.run(run())

    def test_backward_compat_existing_seen_prevents_baseline(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                # Simulate existing deployment: seen_posts.json has URLs, no baseline_state.json
                existing_seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid001"}
                save_seen(existing_seen, seen_file)
                mock_bot = AsyncMock()
                mock_bot.send_message.return_value = True
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                # Backward-compat check (mimics main())
                if seen and not baseline_state["baseline_completed"]:
                    baseline_state["baseline_completed"] = True
                    save_baseline_state(baseline_state, baseline_file)
                posts = self._make_posts(["001", "002"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                # Ambiguous visible posts are snapshotted as historical, not sent.
                self.assertEqual(sent, 0)
                self.assertFalse(
                    any("pfbid002" in call.kwargs.get("text", "")
                        for call in mock_bot.send_message.call_args_list)
                )
                self.assertTrue(baseline_state["baseline_completed"])
                self.assertTrue(baseline_state["history_migration_completed"])
        asyncio.run(run())

    def test_partial_batch_failure_retried_as_new_post(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                # The single selected latest post fails and remains pending.
                mock_bot.send_message.side_effect = Exception("Network error")
                seen = load_seen(seen_file)
                baseline_state = load_baseline_state(baseline_file)
                posts = self._make_posts(["001", "002", "003"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=True
                )
                self.assertEqual(sent, 0)
                self.assertEqual(len(seen), 0)
                self.assertNotIn("https://www.facebook.com/posts/pfbid001", seen)
                self.assertTrue(baseline_state["baseline_completed"])
                self.assertEqual(len(baseline_state["historical_urls"]), 2)
                self.assertEqual(
                    [post["url"] for post in baseline_state["pending_posts"]],
                    ["https://www.facebook.com/posts/pfbid001"],
                )
                mock_bot.reset_mock()
                # Next scan retries the persisted selected post, despite it not
                # being present as a new post.
                mock_bot.send_message.side_effect = None
                mock_bot.send_message.return_value = True
                _, sent_retry = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent_retry, 1)
                self.assertIn("https://www.facebook.com/posts/pfbid001", seen)
        asyncio.run(run())

    def test_image_posts_sent_as_photo(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                baseline_file = Path(tmpdir) / "baseline_state.json"
                mock_bot = AsyncMock()
                mock_bot.send_photo.return_value = True
                seen = {"https://www.facebook.com/fmcs.univbba/posts/pfbid000"}
                baseline_state = {
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "last_successful_scan_at": "2026-09-01T00:00:00+00:00",
                    "historical_urls": [],
                    "pending_posts": [],
                }
                posts = self._make_posts_with_images(["NEW", "000"])
                _, sent = await process_posts_scan(
                    posts=posts, seen=seen, baseline_state=baseline_state,
                    bot=mock_bot, chat_id=123, filepath=seen_file,
                    baseline_filepath=baseline_file, send_initial_batch_on_start=False
                )
                self.assertEqual(sent, 1)
                mock_bot.send_photo.assert_called_once()
                self.assertIn("pfbidNEW", mock_bot.send_photo.call_args.kwargs["caption"])
        asyncio.run(run())

    def test_extraction_failure_preserves_seen(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                seen_file = Path(tmpdir) / "seen_posts.json"
                initial = {"https://www.facebook.com/posts/pfbid001"}
                save_seen(initial, seen_file)
                mock_page = AsyncMock()
                mock_page.goto.side_effect = Exception("Timeout 60000ms exceeded")
                with self.assertRaises(Exception):
                    await extract_posts(mock_page)
                loaded = load_seen(seen_file)
                self.assertEqual(loaded, initial)
        asyncio.run(run())

    def test_non_success_facebook_response_fails_extraction(self):
        async def run():
            page = AsyncMock()
            page.goto.return_value = SimpleNamespace(status=503)
            page.url = "https://www.facebook.com/"

            with self.assertRaisesRegex(
                RuntimeError,
                "Facebook page request failed with HTTP status 503",
            ):
                await extract_posts(page)

            page.locator.assert_not_called()
        asyncio.run(run())

    def test_ordering_newest_first(self):
        posts = [
            {"url": "https://www.facebook.com/fmcs.univbba/posts/pfbidNEW", "text": "Newest"},
            {"url": "https://www.facebook.com/fmcs.univbba/posts/pfbidOLD", "text": "Older"},
            {"url": "https://www.facebook.com/fmcs.univbba/posts/pfbidNEW", "text": "Comment"},
        ]
        unique = {}
        for p in posts:
            unique.setdefault(p["url"], p)
        res = list(unique.values())
        self.assertEqual(res[0]["url"], "https://www.facebook.com/fmcs.univbba/posts/pfbidNEW")
        self.assertEqual(res[0]["text"], "Newest")


class TestFatalMigrationPersistence(unittest.TestCase):

    def test_migration_save_failure_prevents_post_delivery(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                state = {
                    "baseline_completed": True,
                    "history_migration_completed": False,
                    "historical_urls": [],
                    "pending_posts": [],
                }
                seen = {"https://www.facebook.com/posts/pfbid-delivered"}
                bot = AsyncMock()
                post = {
                    "url": "https://www.facebook.com/posts/pfbid-unseen",
                    "text": "Must not be sent",
                    "image_url": None,
                    "timestamp": "N/A",
                }
                with patch(
                    "monitor.save_baseline_state",
                    side_effect=OSError("disk full"),
                ):
                    with self.assertRaises(FatalStatePersistenceError):
                        await process_posts_scan(
                            posts=[post],
                            seen=seen,
                            bot=bot,
                            chat_id=123,
                            filepath=Path(tmpdir) / "seen_posts.json",
                            baseline_filepath=Path(tmpdir) / "baseline_state.json",
                            baseline_state=state,
                        )

                bot.send_message.assert_not_awaited()
                bot.send_photo.assert_not_awaited()
                self.assertFalse(state["history_migration_completed"])
                self.assertEqual(state["historical_urls"], [])
        asyncio.run(run())

    def test_fatal_migration_persistence_error_terminates_monitoring_loop(self):
        async def run():
            class AsyncContextManager:
                def __init__(self, value):
                    self.value = value

                async def __aenter__(self):
                    return self.value

                async def __aexit__(self, exc_type, exc_value, traceback):
                    return False

            app = MagicMock()
            app.__aenter__.return_value = app
            app.__aexit__.return_value = False
            app.running = True
            app.start = AsyncMock()
            app.stop = AsyncMock()
            app.updater.running = True
            app.updater.start_polling = AsyncMock()
            app.updater.stop = AsyncMock()
            builder = MagicMock()
            builder.token.return_value = builder
            builder.connect_timeout.return_value = builder
            builder.read_timeout.return_value = builder
            builder.write_timeout.return_value = builder
            builder.build.return_value = app

            page = MagicMock()
            page.is_closed.return_value = False
            page.close = AsyncMock()
            browser = MagicMock()
            browser.new_page = AsyncMock(return_value=page)
            browser.close = AsyncMock()
            playwright = SimpleNamespace(
                chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
            )

            def fake_getenv(name, default=None):
                return {"BOT_TOKEN": "test-token", "CHAT_ID": "123"}.get(name, default)

            with (
                patch("monitor.load_dotenv"),
                patch("monitor.os.getenv", side_effect=fake_getenv),
                patch("monitor.load_seen", return_value=set()),
                patch("monitor.load_baseline_state", return_value={
                    "baseline_completed": True,
                    "history_migration_completed": False,
                    "historical_urls": [],
                    "pending_posts": [],
                }),
                patch("monitor.save_seen"),
                patch("monitor.ApplicationBuilder", return_value=builder),
                patch(
                    "monitor.async_playwright",
                    return_value=AsyncContextManager(playwright),
                ),
                patch(
                    "monitor.send_telegram_message_with_retry",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
                patch(
                    "monitor.extract_posts",
                    new_callable=AsyncMock,
                    return_value=[{
                        "url": "https://www.facebook.com/posts/pfbid-unseen",
                        "text": "Must not be sent",
                        "image_url": None,
                        "timestamp": "N/A",
                    }],
                ) as extract_mock,
                patch(
                    "monitor.process_posts_scan",
                    new_callable=AsyncMock,
                    side_effect=FatalStatePersistenceError("disk full"),
                ) as process_mock,
            ):
                with self.assertRaises(FatalStatePersistenceError):
                    await monitor.main()

            extract_mock.assert_awaited_once()
            process_mock.assert_awaited_once()
            browser.close.assert_awaited_once()
        asyncio.run(run())

    def test_real_migration_save_failure_escapes_main_without_delivery(self):
        async def run():
            class AsyncContextManager:
                def __init__(self, value):
                    self.value = value

                async def __aenter__(self):
                    return self.value

                async def __aexit__(self, exc_type, exc_value, traceback):
                    return False

            app = MagicMock()
            app.bot = AsyncMock()
            app.__aenter__.return_value = app
            app.__aexit__.return_value = False
            app.running = True
            app.start = AsyncMock()
            app.stop = AsyncMock()
            app.updater.running = True
            app.updater.start_polling = AsyncMock()
            app.updater.stop = AsyncMock()
            builder = MagicMock()
            builder.token.return_value = builder
            builder.connect_timeout.return_value = builder
            builder.read_timeout.return_value = builder
            builder.write_timeout.return_value = builder
            builder.build.return_value = app

            page = MagicMock()
            page.is_closed.return_value = False
            page.close = AsyncMock()
            browser = MagicMock()
            browser.new_page = AsyncMock(return_value=page)
            browser.close = AsyncMock()
            playwright = SimpleNamespace(
                chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
            )
            state = {
                "baseline_completed": True,
                "history_migration_completed": False,
                "historical_urls": [],
                "pending_posts": [],
            }
            visible_post = {
                "url": "https://www.facebook.com/posts/pfbid-unseen",
                "text": "Must not be sent",
                "image_url": None,
                "timestamp": "N/A",
            }

            def fake_getenv(name, default=None):
                return {"BOT_TOKEN": "test-token", "CHAT_ID": "123"}.get(name, default)

            with (
                patch("monitor.load_dotenv"),
                patch("monitor.os.getenv", side_effect=fake_getenv),
                patch("monitor.get_send_initial_batch_on_start", return_value=False),
                patch(
                    "monitor.load_seen",
                    return_value={"https://www.facebook.com/posts/pfbid-delivered"},
                ),
                patch("monitor.load_baseline_state", return_value=state),
                patch("monitor.save_seen"),
                patch("monitor.ApplicationBuilder", return_value=builder),
                patch(
                    "monitor.async_playwright",
                    return_value=AsyncContextManager(playwright),
                ),
                patch(
                    "monitor.send_telegram_message_with_retry",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as startup_notice_mock,
                patch(
                    "monitor.extract_posts",
                    new_callable=AsyncMock,
                    return_value=[visible_post],
                ) as extract_mock,
                patch(
                    "monitor.save_baseline_state",
                    side_effect=OSError("disk full"),
                ) as save_state_mock,
            ):
                with self.assertRaises(FatalStatePersistenceError):
                    await monitor.main()

            extract_mock.assert_awaited_once_with(page)
            save_state_mock.assert_called_once()
            startup_notice_mock.assert_awaited_once()
            self.assertIn(
                "🟢",
                startup_notice_mock.call_args.args[2],
            )
            app.bot.send_message.assert_not_awaited()
            app.bot.send_photo.assert_not_awaited()
            browser.close.assert_awaited_once()
            self.assertFalse(state["history_migration_completed"])
            self.assertEqual(state["historical_urls"], [])
        asyncio.run(run())

    def test_start_command_requests_immediate_scan_through_monitoring_loop(self):
        async def run():
            class AsyncContextManager:
                def __init__(self, value):
                    self.value = value

                async def __aenter__(self):
                    return self.value

                async def __aexit__(self, exc_type, exc_value, traceback):
                    return False

            app = MagicMock()
            app.bot = AsyncMock()
            app.__aenter__.return_value = app
            app.__aexit__.return_value = False
            app.running = True
            app.start = AsyncMock()
            app.stop = AsyncMock()
            app.updater.running = True
            registered_handlers = []
            app.add_handler.side_effect = registered_handlers.append

            app.updater.start_polling = AsyncMock()
            app.updater.stop = AsyncMock()
            builder = MagicMock()
            builder.token.return_value = builder
            builder.connect_timeout.return_value = builder
            builder.read_timeout.return_value = builder
            builder.write_timeout.return_value = builder
            builder.build.return_value = app

            page = MagicMock()
            page.is_closed.return_value = False
            page.close = AsyncMock()
            browser = MagicMock()
            browser.new_page = AsyncMock(return_value=page)
            browser.close = AsyncMock()
            playwright = SimpleNamespace(
                chromium=SimpleNamespace(launch=AsyncMock(return_value=browser))
            )

            def fake_getenv(name, default=None):
                return {"BOT_TOKEN": "test-token", "CHAT_ID": "123"}.get(name, default)

            async def process_and_dispatch_start(*args, **kwargs):
                if process_mock.await_count == 1:
                    await registered_handlers[0].callback(
                        SimpleNamespace(),
                        SimpleNamespace(),
                    )
                return 0, 0

            wait_count = 0

            async def wait_for_or_cancel(awaitable, timeout):
                nonlocal wait_count
                wait_count += 1
                if wait_count == 1:
                    return await awaitable
                awaitable.close()
                raise asyncio.CancelledError

            with (
                patch("monitor.load_dotenv"),
                patch("monitor.os.getenv", side_effect=fake_getenv),
                patch("monitor.load_seen", return_value=set()),
                patch("monitor.load_baseline_state", return_value={
                    "baseline_completed": True,
                    "history_migration_completed": True,
                    "latest_post_startup_migration_completed": True,
                    "historical_urls": [],
                    "pending_posts": [],
                }),
                patch("monitor.save_seen"),
                patch("monitor.ApplicationBuilder", return_value=builder),
                patch(
                    "monitor.CommandHandler",
                    side_effect=lambda command, callback: SimpleNamespace(
                        command=command,
                        callback=callback,
                    ),
                ),
                patch(
                    "monitor.async_playwright",
                    return_value=AsyncContextManager(playwright),
                ),
                patch(
                    "monitor.send_telegram_message_with_retry",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as startup_notice_mock,
                patch(
                    "monitor.extract_posts",
                    new_callable=AsyncMock,
                    return_value=[],
                ) as extract_mock,
                patch(
                    "monitor.process_posts_scan",
                    new_callable=AsyncMock,
                    side_effect=process_and_dispatch_start,
                ) as process_mock,
                patch(
                    "monitor.asyncio.wait_for",
                    side_effect=wait_for_or_cancel,
                ),
            ):
                with self.assertLogs("root", level="INFO") as captured:
                    with self.assertRaises(asyncio.CancelledError):
                        await monitor.main()

            self.assertEqual(registered_handlers[0].command, "start")
            self.assertTrue(any(
                "Telegram /start command received." in line
                for line in captured.output
            ))
            self.assertTrue(any(
                "Immediate Facebook scan requested by /start." in line
                for line in captured.output
            ))
            app.start.assert_awaited_once()
            app.updater.start_polling.assert_awaited_once()
            extract_mock.assert_has_awaits([call(page), call(page)])
            self.assertEqual(process_mock.await_count, 2)
            self.assertTrue(
                process_mock.call_args_list[1].kwargs["send_first_start_post"]
            )
            self.assertIn(
                "تم استلام الطلب",
                startup_notice_mock.call_args_list[1].args[2],
            )
            self.assertIn(
                "لم أتمكن من استخراج أي منشور",
                startup_notice_mock.call_args_list[2].args[2],
            )
            self.assertTrue(all(
                call.kwargs["baseline_state"]["baseline_completed"]
                for call in process_mock.await_args_list
            ))
            app.updater.stop.assert_awaited_once()
            app.stop.assert_awaited_once()
            browser.close.assert_awaited_once()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
