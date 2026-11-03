"""`chat_app` — IBLU as a Google Chat app, so card buttons answer in place.

Every button used to open `/q/<token>` in a browser, behind Google's
"Redirect Notice". These tests pin the app side: Google's signature on an
event is checked before anything is read, a tap records exactly once through
`pings.answers.record_tap` (the one function that records any tap), the card
is rewritten rather than regenerated, a bad token says "expired" and never
raises, and the two card jobs build in-place buttons only when the app is
configured. No network, no database — the Google libraries are patched at
the call sites named in the module.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from iblu_keeper import chat_app as C
from iblu_keeper import chat_app_route as R
from iblu_keeper.jobs import daycard as D
from iblu_keeper.jobs import stage_check as S
from iblu_keeper.pings.deliver import DeliveryError
from iblu_keeper.pings.tokens import InvalidToken, make_token

SECRET = "s" * 32
PROJECT = "123456789012"


# ---------------------------------------------------------------------------
# fixtures: a card as Chat would echo it back, and a connection that is not one
# ---------------------------------------------------------------------------


class _Ctx:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def _two_line_card(token_a: str, token_b: str) -> list:
    """Two lines, each with its own buttons — the stage card's shape."""
    return [{
        "cardId": "stage-7",
        "card": {"sections": [
            {"widgets": [
                {"textParagraph": {"text": "<b>A  Email Writer</b> — deliver"}},
                {"buttonList": {"buttons": [
                    C.build_action_button("→ maintain", token_a),
                    C.build_action_button("stayed deliver", "other-a"),
                ]}},
            ]},
            {"widgets": [
                {"textParagraph": {"text": "<b>B  Scout</b> — build"}},
                {"buttonList": {"buttons": [
                    C.build_action_button("→ trial", token_b),
                ]}},
            ]},
        ]},
    }]


def _clicked(token: str, cards: list, shape: str = "dict") -> dict:
    event = {
        "type": "CARD_CLICKED",
        "message": {
            "name": "spaces/AAA/messages/BBB",
            "text": "@Ignas Did any of these climb a rung this week?",
            "formattedText": "<users/111> Did any of these climb a rung this week?",
            "cardsV2": cards,
        },
    }
    if shape == "dict":
        event["common"] = {"invokedFunction": "tap", "parameters": {"t": token}}
    else:
        event["action"] = {"actionMethodName": "tap",
                           "parameters": [{"key": "t", "value": token}]}
    return event


@pytest.fixture
def recorder(monkeypatch):
    """`record_tap` replaced by a counter that returns a stage result."""
    calls: list[str] = []

    def fake(conn, token):
        calls.append(token)
        return {"qid": "stage", "key": "AU", "label": "Email Writer: deliver → maintain",
                "moved": True, "superseded": False, "project": "email-writer",
                "stage": "maintain"}

    monkeypatch.setattr(C.db, "get_conn", lambda: _Ctx())
    monkeypatch.setattr(C.answers, "record_tap", fake)
    return calls


# ---------------------------------------------------------------------------
# verify_chat_request
# ---------------------------------------------------------------------------


def test_verify_rejects_a_missing_header(monkeypatch):
    monkeypatch.setattr(C.google.oauth2.id_token, "verify_token",
                        lambda *a, **k: pytest.fail("must not reach Google"))
    with pytest.raises(C.ChatAppAuthError):
        C.verify_chat_request("", PROJECT)
    with pytest.raises(C.ChatAppAuthError):
        C.verify_chat_request("Basic abc", PROJECT)


def test_verify_rejects_an_empty_project_number_before_calling_google(monkeypatch):
    """`verify_token` skips the audience check when given none — so an unset
    CHAT_APP_PROJECT_NUMBER must refuse, not accept everything Google signed."""
    monkeypatch.setattr(C.google.oauth2.id_token, "verify_token",
                        lambda *a, **k: pytest.fail("must not reach Google"))
    with pytest.raises(C.ChatAppAuthError):
        C.verify_chat_request("Bearer x.y.z", "")


def test_verify_rejects_a_wrong_audience(monkeypatch):
    def fake(token, request=None, audience=None, certs_url=None):
        raise ValueError("Token has wrong audience")

    monkeypatch.setattr(C.google.oauth2.id_token, "verify_token", fake)
    with pytest.raises(C.ChatAppAuthError):
        C.verify_chat_request("Bearer x.y.z", PROJECT)


def test_verify_rejects_a_wrong_issuer(monkeypatch):
    monkeypatch.setattr(C.google.oauth2.id_token, "verify_token",
                        lambda *a, **k: {"iss": "https://evil.example", "email": "x@y"})
    with pytest.raises(C.ChatAppAuthError):
        C.verify_chat_request("Bearer x.y.z", PROJECT)


def test_verify_accepts_the_chat_issuer_for_our_project_number(monkeypatch):
    seen = {}

    def fake(token, request=None, audience=None, certs_url=None):
        seen.update(token=token, audience=audience, certs_url=certs_url)
        return {"iss": C.CHAT_ISSUER, "aud": audience}

    monkeypatch.setattr(C.google.oauth2.id_token, "verify_token", fake)
    claims = C.verify_chat_request("Bearer x.y.z", PROJECT)
    assert claims["iss"] == C.CHAT_ISSUER
    assert seen == {"token": "x.y.z", "audience": PROJECT, "certs_url": C.CHAT_CERTS_URL}


# ---------------------------------------------------------------------------
# handle_event: CARD_CLICKED
# ---------------------------------------------------------------------------


def test_card_click_records_once_and_rewrites_only_the_tapped_line(recorder):
    token_a = make_token(7, "stage", "AU", SECRET)
    token_b = make_token(7, "stage", "BU", SECRET)
    cards = _two_line_card(token_a, token_b)
    before = copy.deepcopy(cards)

    out = C.handle_event(_clicked(token_a, cards))

    assert recorder == [token_a]
    assert out["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    # the mention survives, as markup — not as a literal "@Ignas"
    assert out["text"].startswith("<users/111> ")

    sections = out["cardsV2"][0]["card"]["sections"]
    # line A: text intact, its buttons gone, replaced by the result line
    assert sections[0]["widgets"][0] == before[0]["card"]["sections"][0]["widgets"][0]
    assert "buttonList" not in sections[0]["widgets"][1]
    assert sections[0]["widgets"][1]["textParagraph"]["text"].startswith(
        "✓ Email Writer: deliver → maintain"
    )
    assert "Gain" in sections[0]["widgets"][1]["textParagraph"]["text"]
    # line B: untouched, buttons and all
    assert sections[1] == before[0]["card"]["sections"][1]
    # and the event's own card was not mutated in place
    assert cards == before


def test_card_click_accepts_the_list_shaped_parameters(recorder):
    token_a = make_token(7, "stage", "AU", SECRET)
    cards = _two_line_card(token_a, "tb")
    out = C.handle_event(_clicked(token_a, cards, shape="list"))
    assert recorder == [token_a]
    assert out["actionResponse"]["type"] == "UPDATE_MESSAGE"
    assert "buttonList" not in out["cardsV2"][0]["card"]["sections"][0]["widgets"][1]


def test_an_invalid_token_reads_expired_and_never_raises(monkeypatch):
    def fake(conn, token):
        raise InvalidToken("bad signature")

    monkeypatch.setattr(C.db, "get_conn", lambda: _Ctx())
    monkeypatch.setattr(C.answers, "record_tap", fake)
    cards = _two_line_card("forged", "tb")

    out = C.handle_event(_clicked("forged", cards))

    assert out["actionResponse"] == {"type": "UPDATE_MESSAGE"}
    line = out["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["textParagraph"]["text"]
    assert line == "Link expired — reply in the thread"
    assert "Traceback" not in str(out)
    # the other line keeps its button
    assert "buttonList" in out["cardsV2"][0]["card"]["sections"][1]["widgets"][1]


def test_a_gone_check_and_an_unexpected_failure_both_answer_in_the_card(monkeypatch):
    monkeypatch.setattr(C.db, "get_conn", lambda: _Ctx())

    def gone(conn, token):
        raise C.answers.UnknownPing("stage check 7 no longer exists")

    monkeypatch.setattr(C.answers, "record_tap", gone)
    out = C.handle_event(_clicked("t1", _two_line_card("t1", "t2")))
    line = out["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["textParagraph"]["text"]
    assert line == C.GONE_LINE

    def boom(conn, token):
        raise RuntimeError("connection reset: https://db.internal/secret?key=abc")

    monkeypatch.setattr(C.answers, "record_tap", boom)
    out = C.handle_event(_clicked("t1", _two_line_card("t1", "t2")))
    line = out["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["textParagraph"]["text"]
    assert line == C.FAILED_LINE
    assert "db.internal" not in str(out)


def test_a_click_without_a_card_to_rewrite_answers_in_the_thread(recorder):
    """Chat did not echo the card (or the button is not ours): say the
    result in a new message rather than blanking the message with an update."""
    token_a = make_token(7, "stage", "AU", SECRET)
    event = _clicked(token_a, cards=[])
    out = C.handle_event(event)
    assert out["actionResponse"] == {"type": "NEW_MESSAGE"}
    assert out["text"].startswith("✓ Email Writer")
    assert "cardsV2" not in out


# ---------------------------------------------------------------------------
# handle_event: everything that is not a tap
# ---------------------------------------------------------------------------


def test_a_message_in_a_space_is_answered_with_silence(monkeypatch):
    """His thread replies ("C was Deadlift") arrive as MESSAGE events; an app
    that answered each one would talk over the day card's own corrections."""
    monkeypatch.setattr(C, "record_tap", lambda *a, **k: (_ for _ in ()).throw(AssertionError("recorded")), raising=False)
    out = C.handle_event({"type": "MESSAGE", "space": {"type": "SPACE"},
                          "message": {"text": "C was Deadlift"}})
    assert out == {}


def test_a_direct_message_gets_the_one_line_answer(monkeypatch):
    out = C.handle_event({"type": "MESSAGE", "space": {"type": "DM"},
                          "message": {"text": "hello"}})
    assert out == {"text": C.NOT_A_CHAT}


def test_space_membership_events_and_unknown_types(monkeypatch):
    monkeypatch.setattr(C.answers, "record_tap",
                        lambda *a, **k: pytest.fail("must not record"))
    assert C.handle_event({"type": "ADDED_TO_SPACE"}) == {"text": C.GREETING}
    assert C.handle_event({"type": "REMOVED_FROM_SPACE"}) == {}
    assert C.handle_event({"type": "SOMETHING_NEW"}) == {}
    assert C.handle_event({}) == {}


# ---------------------------------------------------------------------------
# the shared result wording (what /q shows, and what the card line says)
# ---------------------------------------------------------------------------


def test_tap_result_lines_match_the_q_route_per_qid():
    assert C.tap_result_lines({"qid": "daycard", "key": "A", "label": "All correct",
                               "superseded": False, "confirmed": 4}) == (
        "✓ Saved — All correct",
        "Confirmed 4 block(s). The Secretary calendar updates at the next analyst run.",
    )
    assert C.tap_result_lines({"qid": "daycard", "key": "A", "label": "All correct",
                               "superseded": True, "confirmed": 0})[1] == (
        "Already up to date — nothing left to confirm."
    )
    assert C.tap_result_lines({"qid": "daycard", "key": "B", "label": "Something's wrong",
                               "superseded": False, "confirmed": 0}) == (
        "✓ Saved — Something's wrong", "Reply in this thread with what was wrong.",
    )
    assert C.tap_result_lines({"qid": "stage", "key": "AS", "label": "Scout stays at build",
                               "moved": False, "superseded": True}) == (
        "✓ Scout stays at build", "Already recorded at that stage.",
    )
    assert C.tap_result_lines({"qid": "stage", "key": "AS", "label": "Scout stays at build",
                               "moved": False, "superseded": False})[1] == (
        "Noted — the stage is confirmed, not guessed."
    )
    assert C.tap_result_lines({"qid": "q1", "key": "B", "label": "Deadlift",
                               "superseded": True}) == (
        "✓ Saved — B · Deadlift", "Updated your earlier answer.",
    )
    assert C.tap_result_lines({"qid": "attended", "key": "A", "label": "Yes",
                               "superseded": False}) == (
        "✓ Saved — A · Yes", "The Secretary calendar updates at the next analyst run.",
    )


# ---------------------------------------------------------------------------
# outbound
# ---------------------------------------------------------------------------


def test_build_action_button_shape():
    assert C.build_action_button("All correct", "tok.sig") == {
        "text": "All correct",
        "onClick": {"action": {"function": "tap",
                               "parameters": [{"key": "t", "value": "tok.sig"}]}},
    }


class _FakeChat:
    """`build("chat","v1")` stand-in recording the one create() call."""

    def __init__(self, response=None, fail=None):
        self.calls: list[dict] = []
        self.response = response or {"name": "spaces/AAA/messages/MMM",
                                     "thread": {"name": "spaces/AAA/threads/TTT"}}
        self.fail = fail

    def spaces(self):
        return self

    def messages(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def execute(self):
        if self.fail:
            raise self.fail
        return self.response


def _patch_sender(monkeypatch, fake, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text("{}")
    monkeypatch.setattr(C, "settings", SimpleNamespace(
        chat_app_key_file=str(key), secretary_space="spaces/AAA",
    ))
    seen = {}

    def from_file(path, scopes=None, **kw):
        seen.update(path=path, scopes=list(scopes or []))
        return "CREDS"

    monkeypatch.setattr(C.service_account.Credentials, "from_service_account_file",
                        staticmethod(from_file))

    def build(name, version, credentials=None, **kw):
        seen.update(api=(name, version), credentials=credentials)
        return fake

    monkeypatch.setattr(C, "build", build)
    return seen


def test_send_as_app_threads_the_card_and_uses_only_the_bot_scope(monkeypatch, tmp_path):
    fake = _FakeChat()
    seen = _patch_sender(monkeypatch, fake, tmp_path)

    refs = C.send_as_app("spaces/AAA", {"text": "hi", "cardsV2": []}, thread_key="daycard-9")

    assert refs == {"message_ref": "spaces/AAA/messages/MMM",
                    "thread_ref": "spaces/AAA/threads/TTT"}
    assert seen["scopes"] == ["https://www.googleapis.com/auth/chat.bot"]
    assert seen["api"] == ("chat", "v1") and seen["credentials"] == "CREDS"
    (call,) = fake.calls
    assert call["parent"] == "spaces/AAA"
    assert call["messageReplyOption"] == "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    assert call["body"]["thread"] == {"threadKey": "daycard-9"}
    assert call["body"]["text"] == "hi"


def test_send_as_app_without_a_thread_key_starts_a_plain_message(monkeypatch, tmp_path):
    fake = _FakeChat()
    _patch_sender(monkeypatch, fake, tmp_path)
    C.send_as_app("spaces/AAA", {"text": "hi"})
    (call,) = fake.calls
    assert "messageReplyOption" not in call
    assert "thread" not in call["body"]


def test_send_as_app_failures_become_a_scrubbed_delivery_error(monkeypatch, tmp_path):
    fake = _FakeChat(fail=RuntimeError(
        "POST https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRET failed"
    ))
    _patch_sender(monkeypatch, fake, tmp_path)
    with pytest.raises(DeliveryError) as info:
        C.send_as_app("spaces/AAA", {"text": "hi"}, thread_key="x")
    assert "SECRET" not in str(info.value)
    assert "chat.googleapis.com" not in str(info.value)

    # an empty or malformed space is refused before any call is made
    with pytest.raises(DeliveryError):
        C.send_as_app("", {"text": "hi"})
    with pytest.raises(DeliveryError):
        C.send_as_app("AAA", {"text": "hi"})
    assert len(fake.calls) == 1


def test_app_configured_needs_the_key_file_on_disk_and_a_space(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(C, "settings", SimpleNamespace(
        chat_app_key_file=str(missing), secretary_space="spaces/AAA",
    ))
    assert C.app_configured() is False

    missing.write_text("{}")
    assert C.app_configured() is True

    monkeypatch.setattr(C, "settings", SimpleNamespace(
        chat_app_key_file=str(missing), secretary_space="",
    ))
    assert C.app_configured() is False


# ---------------------------------------------------------------------------
# the two card jobs
# ---------------------------------------------------------------------------


def _walk(obj):
    """Every dict reachable from `obj`."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _keys(obj) -> set[str]:
    return {k for d in _walk(obj) for k in d}


def _proposal():
    return {
        "letter": "A", "project": "email-writer", "name": "Email Writer",
        "venture": "blt", "from_stage": "deliver", "from_ordinal": 6,
        "from_since": "2026-09-13T00:00:00+00:00", "next_stage": "maintain",
        "next_label": "Maintaining", "top_stage": "autonomous",
        "evidence": {"signals": 106, "sources": 3, "subjects": ["Email Writer"]},
    }


def _daycard_lines():
    return [{"letter": "A", "line": "A  09:00–11:15  Deadlift · Machina", "mark": None,
             "text": "Deadlift · Machina", "start": "09:00", "end": "11:15",
             "block_ids": [1]}]


def test_daycard_card_modes_are_exclusive():
    app = D.build_card(9, _daycard_lines(), "111", "https://x", SECRET, mode="app")
    link = D.build_card(9, _daycard_lines(), "111", "https://x", SECRET, mode="link")

    assert "openLink" not in _keys(app) and "action" in _keys(app)
    assert "action" not in _keys(link) and "openLink" in _keys(link)
    buttons = app["cardsV2"][0]["card"]["sections"][1]["widgets"][0]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["All correct", "Something's wrong → reply"]
    assert all(b["onClick"]["action"]["function"] == "tap" for b in buttons)
    # the default is the link, so every existing caller is unchanged
    assert D.build_card(9, _daycard_lines(), "111", "https://x", SECRET) == link
    with pytest.raises(ValueError):
        D.build_card(9, _daycard_lines(), "111", "https://x", SECRET, mode="sms")


def test_stage_card_modes_are_exclusive():
    app = S.build_card(7, [_proposal()], "111", "https://x", SECRET, mode="app")
    link = S.build_card(7, [_proposal()], "111", "https://x", SECRET, mode="link")
    assert "openLink" not in _keys(app) and "action" in _keys(app)
    assert "action" not in _keys(link) and "openLink" in _keys(link)
    buttons = app["cardsV2"][0]["card"]["sections"][0]["widgets"][2]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["→ maintain", "→ autonomous", "stayed deliver"]
    assert S.build_card(7, [_proposal()], "111", "https://x", SECRET) == link


@pytest.mark.parametrize("job,thread_key,args", [
    (D, "daycard-9", (9, _daycard_lines(), "111")),
    (S, "stage-7", (7, [_proposal()], "111")),
])
def test_send_prefers_the_app_and_never_touches_the_webhook(monkeypatch, job, thread_key, args):
    monkeypatch.setattr(job, "settings", SimpleNamespace(
        ping_signing_secret=SECRET, mcp_public_base_url="https://x",
        secretary_space="spaces/AAA", secretary_webhook_url="https://hook?key=K",
    ))
    monkeypatch.setattr(C, "app_configured", lambda: True)
    sent = {}

    def fake_send(space, body, thread_key=None):
        sent.update(space=space, body=body, thread_key=thread_key)
        return {"message_ref": "m", "thread_ref": "t"}

    monkeypatch.setattr(C, "send_as_app", fake_send)
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: pytest.fail("app mode must not hit the webhook"))
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: pytest.fail("app mode needs no /q preflight"))

    refs = job.send(*args)

    assert refs == {"message_ref": "m", "thread_ref": "t"}
    assert sent["space"] == "spaces/AAA"
    assert sent["thread_key"] == thread_key
    assert "openLink" not in _keys(sent["body"])
    assert "<users/111>" in sent["body"]["text"]


def test_send_falls_back_to_the_webhook_when_the_app_is_not_configured(monkeypatch):
    monkeypatch.setattr(D, "settings", SimpleNamespace(
        ping_signing_secret=SECRET, mcp_public_base_url="https://x",
        secretary_space="spaces/AAA", secretary_webhook_url="https://hook?key=K",
    ))
    monkeypatch.setattr(C, "app_configured", lambda: False)
    monkeypatch.setattr(C, "send_as_app",
                        lambda *a, **k: pytest.fail("not configured: must not post as app"))
    from iblu_keeper.pings import deliver
    monkeypatch.setattr(deliver, "preflight_tap_route", lambda: None)
    posted = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"name": "m", "thread": {"name": "t"}}

    def post(url, json=None, timeout=None):
        posted.update(url=url, body=json)
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", post)

    refs = D.send(9, _daycard_lines(), "111")
    assert refs == {"message_ref": "m", "thread_ref": "t"}
    assert "threadKey=daycard-9" in posted["url"]
    assert "openLink" in _keys(posted["body"]) and "action" not in _keys(posted["body"])


# ---------------------------------------------------------------------------
# the route
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(R, "settings", SimpleNamespace(chat_app_project_number=PROJECT))
    app = Starlette(routes=[Route("/chat/events", R.chat_events, methods=["POST"])])
    return TestClient(app)


def test_route_returns_401_and_nothing_else_on_bad_auth(client, monkeypatch):
    def reject(header, project):
        raise C.ChatAppAuthError("token has wrong audience 999")

    monkeypatch.setattr(C, "verify_chat_request", reject)
    monkeypatch.setattr(C, "handle_event", lambda e: pytest.fail("must not handle"))

    r = client.post("/chat/events", json={"type": "CARD_CLICKED"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert "999" not in r.text

    r = client.post("/chat/events", json={"type": "CARD_CLICKED"})
    assert r.status_code == 401


def test_route_answers_200_with_text_on_an_internal_error(client, monkeypatch):
    monkeypatch.setattr(C, "verify_chat_request", lambda h, p: {"iss": C.CHAT_ISSUER})

    def boom(event):
        raise RuntimeError("pool exhausted at postgres://db/iblu?password=hunter2")

    monkeypatch.setattr(C, "handle_event", boom)
    r = client.post("/chat/events", json={"type": "CARD_CLICKED"},
                    headers={"Authorization": "Bearer x"})
    assert r.status_code == 200
    assert r.json() == {"text": "IBLU could not process that tap — reply in the thread."}
    assert "hunter2" not in r.text

    # a body that is not JSON is an internal error too, never a 4xx/5xx
    r = client.post("/chat/events", content=b"not json",
                    headers={"Authorization": "Bearer x", "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["text"].startswith("IBLU could not")


def test_route_passes_the_verified_event_through(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(C, "verify_chat_request",
                        lambda h, p: seen.update(header=h, project=p) or {"iss": C.CHAT_ISSUER})
    monkeypatch.setattr(C, "handle_event", lambda e: {"text": f"got {e['type']}"})
    r = client.post("/chat/events", json={"type": "MESSAGE"},
                    headers={"Authorization": "Bearer abc"})
    assert r.status_code == 200
    assert r.json() == {"text": "got MESSAGE"}
    assert seen == {"header": "Bearer abc", "project": PROJECT}
