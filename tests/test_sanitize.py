"""_sanitize_for_tts: strip emoji + markdown noise the LLM emits despite the
system prompt, without mangling normal speech. Offline."""

from __future__ import annotations

from server.session import _sanitize_for_tts


def test_empty_and_none():
    assert _sanitize_for_tts("") == ""
    assert _sanitize_for_tts(None) is None  # guard returns the falsy input


def test_plain_text_passes_through():
    s = "Hey, just a reminder - it's time to call Mom at 9am."
    assert _sanitize_for_tts(s) == s


def test_strips_emoji():
    assert _sanitize_for_tts("Hello there \U0001F44B") == "Hello there"
    assert "\U0001F600" not in _sanitize_for_tts("happy \U0001F600 day")


def test_strips_markdown_emphasis():
    assert _sanitize_for_tts("this is *very* _important_ and `code`") == \
        "this is very important and code"


def test_collapses_whitespace_left_by_emoji_removal():
    # "fresh 👋 here" -> "fresh  here" -> "fresh here"
    assert _sanitize_for_tts("fresh \U0001F44B here") == "fresh here"


def test_idempotent():
    once = _sanitize_for_tts("**bold** \U0001F680 launch")
    assert _sanitize_for_tts(once) == once


def test_trims_outer_whitespace():
    assert _sanitize_for_tts("  spaced out  ") == "spaced out"


def test_keeps_numbers_and_punctuation():
    s = "Set for 2026-06-10 at 09:00 (sharp)!"
    assert _sanitize_for_tts(s) == s
