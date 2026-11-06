"""Writer: the writing rules are checked and fixed in code, not just asked for in the prompt."""

from delta.addons.writer.rules import (
    AI_WORDS,
    check_text,
    fix_mechanical,
    limit_hashtags,
    rhythm_issue,
    strip_markdown,
)


def codes(text):
    return {i.code for i in check_text(text)}


def test_em_and_en_dashes_are_flagged_and_fixed():
    text = "We shipped it \u2014 finally. Pages 10\u201312 cover it. Wait -- really."
    assert "dash" in codes(text)
    fixed = fix_mechanical(text)
    assert "\u2014" not in fixed and "\u2013" not in fixed and "--" not in fixed
    assert "10 to 12" in fixed and "dash" not in codes(fixed)


def test_emojis_are_flagged_and_removed():
    text = "Big news today \U0001F680\U0001F525 and more \u2728"
    assert "emoji" in codes(text)
    assert "emoji" not in codes(fix_mechanical(text))
    assert fix_mechanical(text) == "Big news today and more"


def test_backslashes_are_flagged_and_removed_but_urls_and_code_are_kept():
    text = "Path C:\\Users is shown \\n here. See `a\\b` and https://x.in/a\\b"
    assert "backslash" in codes("C:\\Users here")
    fixed = fix_mechanical(text)
    assert "C:Users" in fixed and "`a\\b`" in fixed and "https://x.in/a\\b" in fixed


def test_ai_words_are_flagged_and_plain_swaps_applied():
    text = "In today's fast-paced world, we leverage AI. Furthermore, it is a game-changer that will delve into data."
    issues = [i for i in check_text(text) if i.code == "ai_word"]
    assert issues and "leverage" in issues[0].message
    fixed = fix_mechanical(text)
    for word in ("leverage", "game-changer", "fast-paced", "Furthermore", "delve"):
        assert word.lower() not in fixed.lower()
    assert fixed.startswith("We use AI.") and "It is a big shift" in fixed


def test_not_just_pattern_is_reported_not_rewritten():
    text = "It's not just a tool, it's a partner."
    assert any("not just" in i.message for i in check_text(text))
    assert fix_mechanical(text) == text  # meaning-changing rewrites are left to the model


def test_us_spellings_become_indian_english_with_case_kept():
    text = "Organize the Color workshop at the center; we analyzed behavior and prioritized it."
    assert "us_spelling" in codes(text)
    fixed = fix_mechanical(text)
    assert fixed == "Organise the Colour workshop at the centre; we analysed behaviour and prioritised it."
    assert "us_spelling" not in codes(fixed)


def test_spelling_is_not_touched_inside_urls_or_code():
    text = "Read https://example.com/color-center and run `organize.py`."
    assert fix_mechanical(text) == text


def test_program_is_left_for_the_writer_to_choose():
    assert fix_mechanical("I wrote a program for the programme.") == "I wrote a program for the programme."


def test_hashtag_limit_and_markdown_stripping():
    assert limit_hashtags("Hi\n\n#ai #ml #python #tech #india", 3) == "Hi\n\n#ai #ml #python"
    assert strip_markdown("**Bold** and [link](https://a.in)\n# Head") == "Bold and link (https://a.in)\nHead"
    assert strip_markdown("run my_script_name now, *really*") == "run my_script_name now, really"
    tagged = "Last line of the post.\n\n#BuildInPublic #StudentDevelopers"
    assert fix_mechanical(tagged, markdown=False, hashtags=3) == tagged  # hashtags are not headings


def test_fix_is_idempotent_and_clean_text_passes():
    clean = "I built a small tool last week. It saves me an hour a day. Try it and tell me what breaks."
    assert check_text(clean) == []
    once = fix_mechanical("We leverage AI \u2014 daily \U0001F680. Organize it.")
    assert fix_mechanical(once) == once


def test_flat_rhythm_is_reported():
    flat = " ".join(["This sentence has exactly seven words here."] * 7)
    assert rhythm_issue(flat) is not None
    varied = ("Short one. This next sentence goes on for quite a bit longer than the first one did. Yes. "
              "Then a medium sentence follows here. Tiny. And finally a long closing line that wraps everything "
              "up for the reader nicely.")
    assert rhythm_issue(varied) is None


def test_ai_word_list_is_maintained():
    for w in ("delve", "tapestry", "leverage", "unlock", "game-changer", "in today's fast-paced world"):
        assert w in AI_WORDS


def test_a_zero_width_joiner_in_indic_text_is_not_an_emoji():
    from delta.addons.writer.rules import check_text, fix_mechanical

    word = "\u0c28\u0c4d\u200d\u0c28"  # Telugu with a ZWJ: part of the script
    assert not any(i.code == "emoji" for i in check_text(f"{word} ok"))
    assert word in fix_mechanical(f"{word} ok")
    assert fix_mechanical("family \U0001F468\u200d\U0001F469 ok") == "family ok"
