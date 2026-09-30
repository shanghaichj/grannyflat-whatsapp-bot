from grannyflat_bot.channels.whatsapp_stub import WhatsAppChannelStub


def test_parse_meta_webhook():
    stub = WhatsAppChannelStub()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "61400000000",
                                    "type": "text",
                                    "text": {"body": "Hello"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    msgs = stub.parse_meta_webhook(payload)
    assert len(msgs) == 1
    assert msgs[0].text == "Hello"
    assert msgs[0].session_id == "wa-61400000000"


def test_parse_twilio_form():
    stub = WhatsAppChannelStub()
    msg = stub.parse_twilio_form(
        {"From": "whatsapp:+61411111111", "Body": "Quote please"}
    )
    assert msg.text == "Quote please"
    assert msg.sender_id == "+61411111111"
