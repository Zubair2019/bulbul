"""voicecat end to end (minus the network): turns -> brain -> TTS -> transport."""

from __future__ import annotations

import asyncio

from tests.conftest import FakeBulbul, Harness, tokens, wait_for
from voicecat.frames import Interruption, Transcript


async def test_a_spoken_turn_end_to_end():
    bulbul = FakeBulbul([tokens("Hi Asha. Your cart has two items for about three hundred rupees.")])
    async with Harness(bulbul) as h:
        await h.put(Transcript("what's in my cart", typed=True))
        await wait_for(lambda: bulbul.finished == 1 and "rupees" in h.spoken())
        await wait_for(lambda: h.session.state == "listening" and len(h.events("state")) >= 3)
        assert bulbul.requests == [("runs", {"message": "what's in my cart", "voice": True})]
        assert h.events("user_turn")[0]["text"] == "what's in my cart"
        assert "Your cart has two items" in h.spoken()
        assert h.session.sent_audio, "speech audio was sent"
        states = [e["state"] for e in h.events("state")]
        assert states[:3] == ["thinking", "speaking", "listening"]


async def test_filler_covers_the_silence_while_an_agent_works():
    bulbul = FakeBulbul([[(0.01, {"type": "agent_start", "agent": "shopping", "goal": "check"}),
                          *tokens("Everything is in stock.", delay=0.05)]])
    async with Harness(bulbul) as h:
        await h.put(Transcript("check instamart for biryani things", typed=True))
        await wait_for(lambda: bulbul.finished == 1)
        await wait_for(lambda: "Everything is in stock." in h.spoken())
        said = [e["text"] for e in h.events("bot_text")]
        assert said[0] == "Let me check Instamart."


async def test_barge_in_stops_speech_but_lets_bulbul_finish():
    long_reply = " ".join(f"Sentence number {i} of a long reply." for i in range(12))
    bulbul = FakeBulbul([tokens(long_reply, delay=0.02), tokens("Sure, adding eggs.")])
    async with Harness(bulbul) as h:
        await h.put(Transcript("tell me everything", typed=True))
        await wait_for(lambda: h.session.bot_speaking)
        await h.put(Interruption())                       # the driver talks over Bulbul
        assert h.events("interrupt"), "client told to stop playback now"
        assert not h.session.bot_speaking
        spoken_at_interrupt = len(h.events("bot_text"))
        await h.put(Transcript("add eggs too", typed=True))  # what they said
        await wait_for(lambda: bulbul.finished == 2, timeout=5)
        # The first request was NOT cancelled: it ran to completion, quietly.
        assert ("cancel", {}) not in bulbul.requests
        assert [r[0] for r in bulbul.requests] == ["runs", "runs"]
        assert bulbul.requests[1][1]["message"] == "add eggs too"
        await wait_for(lambda: "Sure, adding eggs." in h.spoken())
        rest_of_first = [e["text"] for e in h.events("bot_text")[spoken_at_interrupt:]]
        assert not any("Sentence number 11" in t for t in rest_of_first)


async def test_talking_while_bulbul_thinks_is_handled_right_after():
    bulbul = FakeBulbul([[(0.3, {"type": "token", "text": "Here is the long answer. "})],
                         tokens("Okay, adding milk.")])
    async with Harness(bulbul) as h:
        await h.put(Transcript("plan my week", typed=True))
        await wait_for(lambda: h.session.brain_busy)
        await h.put(Transcript("also add milk", typed=True))
        await wait_for(lambda: h.events("queued"))
        assert h.events("queued")[0]["text"] == "also add milk"
        assert len(bulbul.requests) == 1                    # not sent while Bulbul is busy
        await wait_for(lambda: bulbul.finished == 2 and "Okay, adding milk." in h.spoken())
        assert bulbul.requests[1][1]["message"] == "also add milk"
        assert "Here is the long answer." not in h.spoken()   # finished quietly, kept in the chat


CART_CARD = {"type": "interrupt", "kind": "approve", "step_id": "s1", "agent": "shopping", "actions": [
    {"kind": "update_cart", "tool": "update_cart", "description": "Replace your cart…", "items": [
        {"spin_id": "chicken-1", "name": "Chicken Curry Cut (with bone)", "size": "500 g", "price": 189, "quantity": 1},
        {"spin_id": "rice-1", "name": "Basmati Rice", "size": "1 kg", "price": 145, "quantity": 2},
        {"spin_id": "curd-1", "name": "Fresh Curd (Dahi)", "size": "400 g", "price": 35, "quantity": 1},
        {"spin_id": "mint-1", "name": "Mint Leaves (Pudina)", "size": "100 g", "price": 15, "quantity": 1}]}]}


async def test_a_cart_card_is_spoken_briefly_and_answered_by_voice():
    bulbul = FakeBulbul([[(0.01, CART_CARD)], tokens("Done, your cart is updated.")])
    async with Harness(bulbul) as h:
        await h.put(Transcript("buy the biryani ingredients", typed=True))
        await wait_for(lambda: bulbul.finished == 1 and h.events("card"))
        await wait_for(lambda: "Shall I add them" in h.spoken())
        said = h.spoken()
        assert "4 items" in said and "Chicken Curry Cut, Basmati Rice, Fresh Curd and more" in said
        assert "about 529 rupees" in said                 # 189 + 2x145 + 35 + 15
        await h.put(Transcript("yes but skip the curd", typed=True))
        await wait_for(lambda: bulbul.finished == 2)
        assert bulbul.requests[1] == ("resume", {"value": {"edit": "yes but skip the curd"}})


async def test_order_and_question_cards_by_voice():
    checkout = {"type": "interrupt", "kind": "approve", "actions": [{"kind": "checkout", "tool": "checkout",
                "description": "Place the Instamart order: 18 items, ₹1,424 to pay, paid by cash on delivery, "
                               "delivered to Home (12 Lake View Road, Indiranagar)."}]}
    question = {"type": "interrupt", "kind": "clarify", "questions": [
        {"id": "dish", "text": "Which dish would you like to make?", "options": ["Biryani", "Chole", "Paneer"]}]}
    bulbul = FakeBulbul([[(0.01, checkout)], tokens("Order placed."), [(0.01, question)], tokens("Great choice.")])
    async with Harness(bulbul) as h:
        await h.put(Transcript("place the order", typed=True))
        await wait_for(lambda: "Shall I place it?" in h.spoken())
        assert ("Ready to place your order: 18 items, 1424 rupees, cash on delivery, delivered to Home."
                in h.spoken())
        await h.put(Transcript("yes place it", typed=True))
        await wait_for(lambda: bulbul.finished == 2)
        assert bulbul.requests[1] == ("resume", {"value": {"edit": "yes place it"}})

        await h.put(Transcript("let's cook something", typed=True))
        await wait_for(lambda: "You can say Biryani, Chole or Paneer." in h.spoken())
        await h.put(Transcript("biryani", typed=True))
        await wait_for(lambda: bulbul.finished == 4)
        assert bulbul.requests[3] == ("resume", {"value": {"answers": {"_free_text": "biryani"}}})


async def test_resumes_a_chat_that_is_waiting_on_a_card():
    bulbul = FakeBulbul([tokens("Added.")], pending={k: v for k, v in CART_CARD.items() if k != "type"})
    async with Harness(bulbul) as h:
        await h.put(Transcript("yes", typed=True))
        await wait_for(lambda: bulbul.finished == 1)
        assert bulbul.requests[0] == ("resume", {"value": {"edit": "yes"}})


async def test_bulbul_unreachable_is_said_out_loud():
    class Down(FakeBulbul):
        def handler(self, request):
            if request.url.path.endswith("/runs"):
                import httpx

                return httpx.Response(502, text="bad gateway")
            return super().handler(request)

    async with Harness(Down([])) as h:
        await h.put(Transcript("hello", typed=True))
        await wait_for(lambda: "couldn't reach Bulbul" in h.spoken() and h.events("error"))
        await asyncio.sleep(0.1)
        assert not h.session.brain_busy
