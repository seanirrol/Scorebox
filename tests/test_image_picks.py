#!/usr/bin/env python3
"""
Regression tests for image_picks.py - the Claude-vision adapter that
transcribes a picks-slate graphic into the bracket-tagged plain text
picks.parse_picks_message already understands. The actual Anthropic API
call is mocked throughout; these tests only cover this module's own
wrapping/error-handling logic, not Claude's own transcription quality.

Run with: python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

import image_picks
import picks


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class ExtractPicksText(unittest.TestCase):
    def test_returns_none_when_no_client_configured(self):
        with patch.object(image_picks, "_client", None):
            result = _run(image_picks.extract_picks_text(b"fake-image-bytes", "image/png"))
        self.assertIsNone(result)

    def test_returns_transcribed_text_on_success(self):
        with patch.object(image_picks, "_client", MagicMock()), \
             patch.object(image_picks, "_extract_sync", return_value="[MLB] Cincinnati Reds F5 ML"):
            result = _run(image_picks.extract_picks_text(b"fake-image-bytes", "image/png"))
        self.assertEqual(result, "[MLB] Cincinnati Reds F5 ML")

    def test_api_error_returns_none_instead_of_raising(self):
        # A failed/rate-limited call must never take down on_message's
        # whole picks pipeline - the message's own text content (if any)
        # should still get parsed normally.
        with patch.object(image_picks, "_client", MagicMock()), \
             patch.object(image_picks, "_extract_sync", side_effect=anthropic.APIError("boom", request=MagicMock(), body=None)):
            result = _run(image_picks.extract_picks_text(b"fake-image-bytes", "image/png"))
        self.assertIsNone(result)

    def test_empty_model_response_is_a_valid_falsy_result_not_none(self):
        # Distinct from "not configured"/"API failed" - the model looked
        # at the image and found nothing parseable in it.
        with patch.object(image_picks, "_client", MagicMock()), \
             patch.object(image_picks, "_extract_sync", return_value=""):
            result = _run(image_picks.extract_picks_text(b"fake-image-bytes", "image/png"))
        self.assertEqual(result, "")

    def test_dropped_bracket_from_the_model_gets_normalized(self):
        # Confirmed live against a real test image: despite the prompt's
        # explicit instruction, the model sometimes drops the bracket tag
        # entirely ("MLB Cincinnati Reds F5 ML") - parse_picks_message
        # can't use that line at all without the safety net normalizing
        # it back into bracket form.
        with patch.object(image_picks, "_client", MagicMock()), \
             patch.object(image_picks, "_extract_sync", return_value="MLB Cincinnati Reds F5 ML\nWNBA Atlanta Dream ML"):
            result = _run(image_picks.extract_picks_text(b"fake-image-bytes", "image/png"))
        self.assertEqual(result, "[MLB] Cincinnati Reds F5 ML\n[WNBA] Atlanta Dream ML")
        # And it actually parses now, which is the whole point.
        self.assertEqual(len(picks.parse_picks_message(result)), 2)


class SniffMediaType(unittest.TestCase):
    """_sniff_media_type backs extract_picks_text's own correction of
    Discord's attachment.content_type - confirmed live, Discord reported
    "image/jpeg" for a real .png upload (filename and all), and Anthropic's
    API rejects that mismatch outright with a 400 on every single call, so
    every image from that source silently failed to transcribe."""

    def test_png_magic_bytes_detected_even_if_discord_claimed_jpeg(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"rest of a real png file"
        self.assertEqual(image_picks._sniff_media_type(png_bytes, "image/jpeg"), "image/png")

    def test_jpeg_magic_bytes_detected_even_if_discord_claimed_png(self):
        jpeg_bytes = b"\xff\xd8\xff" + b"rest of a real jpeg file"
        self.assertEqual(image_picks._sniff_media_type(jpeg_bytes, "image/png"), "image/jpeg")

    def test_gif_magic_bytes_detected(self):
        gif_bytes = b"GIF89a" + b"rest of a real gif file"
        self.assertEqual(image_picks._sniff_media_type(gif_bytes, "image/png"), "image/gif")

    def test_webp_magic_bytes_detected(self):
        webp_bytes = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest of a real webp file"
        self.assertEqual(image_picks._sniff_media_type(webp_bytes, "image/png"), "image/webp")

    def test_unrecognized_bytes_fall_back_to_discords_own_claim(self):
        self.assertEqual(image_picks._sniff_media_type(b"not a real image", "image/png"), "image/png")

    def test_extract_picks_text_uses_the_sniffed_type_not_discords_claim(self):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"rest of a real png file"
        with patch.object(image_picks, "_client", MagicMock()), \
             patch.object(image_picks, "_extract_sync", return_value="[MLB] Cincinnati Reds F5 ML") as fake_sync:
            _run(image_picks.extract_picks_text(png_bytes, "image/jpeg"))
        fake_sync.assert_called_once_with(png_bytes, "image/png")


class NormalizeLine(unittest.TestCase):
    def test_already_bracketed_line_is_unchanged(self):
        self.assertEqual(image_picks._normalize_line("[MLB] Cincinnati Reds F5 ML"), "[MLB] Cincinnati Reds F5 ML")

    def test_missing_bracket_gets_wrapped(self):
        self.assertEqual(image_picks._normalize_line("MLB Cincinnati Reds F5 ML"), "[MLB] Cincinnati Reds F5 ML")

    def test_two_word_section_prefers_longer_match(self):
        self.assertEqual(image_picks._normalize_line("Dota 2 Team A ML"), "[Dota 2] Team A ML")

    def test_unrecognized_leading_word_is_left_alone(self):
        line = "Cincinnati Reds F5 ML"
        self.assertEqual(image_picks._normalize_line(line), line)

    def test_blank_line_is_left_alone(self):
        self.assertEqual(image_picks._normalize_line(""), "")

    def test_section_word_with_nothing_after_it_is_left_alone(self):
        # Just a bare "MLB" on its own line - not a pick to wrap, and
        # picks.py's own parser skips a bracket-less bare header anyway.
        self.assertEqual(image_picks._normalize_line("MLB"), "MLB")


class ExtractSync(unittest.TestCase):
    """_extract_sync itself, with the Anthropic client's create() call
    mocked - covers the request shape and response-joining logic."""

    def _fake_response(self, text):
        block = MagicMock()
        block.type = "text"
        block.text = text
        response = MagicMock()
        response.content = [block]
        return response

    def test_joins_multiple_text_blocks(self):
        block1 = MagicMock(type="text", text="[MLB] Cincinnati Reds F5 ML\n")
        block2 = MagicMock(type="text", text="[WNBA] Atlanta Dream ML")
        response = MagicMock()
        response.content = [block1, block2]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response
        with patch.object(image_picks, "_client", fake_client):
            result = image_picks._extract_sync(b"fake-bytes", "image/png")
        self.assertEqual(result, "[MLB] Cincinnati Reds F5 ML\n[WNBA] Atlanta Dream ML")

    def test_ignores_non_text_blocks(self):
        text_block = MagicMock(type="text", text="[MLB] Cincinnati Reds F5 ML")
        other_block = MagicMock(type="thinking")
        response = MagicMock()
        response.content = [other_block, text_block]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response
        with patch.object(image_picks, "_client", fake_client):
            result = image_picks._extract_sync(b"fake-bytes", "image/png")
        self.assertEqual(result, "[MLB] Cincinnati Reds F5 ML")

    def test_strips_surrounding_whitespace(self):
        block = MagicMock(type="text", text="\n\n[MLB] Cincinnati Reds F5 ML\n\n")
        response = MagicMock()
        response.content = [block]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response
        with patch.object(image_picks, "_client", fake_client):
            result = image_picks._extract_sync(b"fake-bytes", "image/png")
        self.assertEqual(result, "[MLB] Cincinnati Reds F5 ML")

    def test_passes_base64_image_and_media_type_in_request(self):
        import base64
        response = self._fake_response("[MLB] Cincinnati Reds F5 ML")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = response
        with patch.object(image_picks, "_client", fake_client):
            image_picks._extract_sync(b"hello", "image/jpeg")
        _args, kwargs = fake_client.messages.create.call_args
        image_block = kwargs["messages"][0]["content"][0]
        self.assertEqual(image_block["type"], "image")
        self.assertEqual(image_block["source"]["media_type"], "image/jpeg")
        self.assertEqual(image_block["source"]["data"], base64.b64encode(b"hello").decode("ascii"))


if __name__ == "__main__":
    unittest.main()
