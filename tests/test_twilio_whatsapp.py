from grannyflat_bot.channels.twilio_whatsapp import TwilioWhatsAppChannel


def test_build_twiml_escapes_xml():
    ch = TwilioWhatsAppChannel()
    xml = ch.build_twiml('Hello <world> & "quote"')
    assert "&lt;world&gt;" in xml
    assert "&amp;" in xml
    assert "<Message>" in xml


def test_parse_twilio_form():
    ch = TwilioWhatsAppChannel()
    msg = ch.parse_twilio_form(
        {"From": "whatsapp:+61411111111", "Body": "Quote please"}
    )
    assert msg.text == "Quote please"
    assert msg.sender_id == "+61411111111"
    assert msg.session_id == "wa-+61411111111"
