

def test_small_talk_needs_no_ai():
    from delta.core.intents import small_talk

    assert small_talk("hi", "Adharsh") == "Hi, Adharsh! What should I do for you?"
    for t in ["Hello!", "hey delta", "good morning", "thanks buddy", "thank you so much"]:
        assert small_talk(t), t
    for t in ["hi, open notepad", "hello world in python", "thanks, now play a song"]:
        assert small_talk(t) is None, t
