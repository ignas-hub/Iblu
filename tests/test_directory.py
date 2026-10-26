"""Tests for `tools.directory` — Workspace directory search. Fakes only, no network."""

from __future__ import annotations

import json

import pytest

from iblu_keeper.tools import directory as d


class _Exec:
    def __init__(self, payload=None, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._payload


class _FakePeople:
    def __init__(self, people=None, raise_exc=None):
        self.calls: list[dict] = []
        self._people = people if people is not None else []
        self._raise = raise_exc

    def people(self):
        return self

    def searchDirectoryPeople(self, **kwargs):  # noqa: N802 - googleapiclient name
        self.calls.append(kwargs)
        if self._raise is not None:
            return _Exec(raise_exc=self._raise)
        return _Exec({"people": self._people})


_MARTA = {
    "resourceName": "people/c1",
    "names": [{"displayName": "Marta Kowal", "metadata": {"primary": True}}],
    "emailAddresses": [
        {"value": "marta.old@blanklabel.team"},
        {"value": "marta@blanklabel.team", "metadata": {"primary": True}},
    ],
    "organizations": [{"title": "Head of Finance", "department": "Finance"}],
    "phoneNumbers": [{"value": "+385 91 000 0000"}],
    "photos": [{"url": "https://lh3.example/photo", "default": False}],
}


class _Live:
    use_mock = False


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(d, "settings", _Live())


def _install(monkeypatch, fake):
    monkeypatch.setattr(d, "_people", lambda account=None: fake)
    return fake


def test_mock_mode_never_touches_the_api(monkeypatch):
    """DRY_RUN must mean no network; a service build here is a leak."""
    def _boom(account=None):
        raise AssertionError("service built in mock mode")
    monkeypatch.setattr(d, "_people", _boom)
    result = d.directory_search("marta")
    assert result["_mock"] is True
    assert result["status"] == "not_searched_mock"
    assert result["people"] == []


def test_query_is_required_even_in_mock_mode():
    """An empty query would hand Google a request for everyone in the domain."""
    with pytest.raises(ValueError, match="query"):
        d.directory_search("   ")


def test_unknown_account_is_refused_before_anything_else():
    """A misspelt alias must not silently fall back to the primary Workspace."""
    with pytest.raises(ValueError, match="Unknown account"):
        d.directory_search("marta", account="nope")


def test_request_uses_domain_profile_source_and_the_read_mask(monkeypatch, live):
    """Without the source the API returns nothing; without the mask it returns no fields."""
    fake = _install(monkeypatch, _FakePeople())
    d.directory_search("  marta ")
    assert fake.calls == [{
        "query": "marta",
        "readMask": "names,emailAddresses,organizations,phoneNumbers,photos",
        "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"],
        "pageSize": 10,
    }]


def test_limit_is_clamped_to_1_and_50(monkeypatch, live):
    """pageSize=0 is an API error and pageSize=5000 is the whole company."""
    fake = _install(monkeypatch, _FakePeople())
    d.directory_search("m", limit=0)
    d.directory_search("m", limit=5000)
    assert [c["pageSize"] for c in fake.calls] == [1, 50]


def test_person_is_flattened_to_the_fields_the_question_needs(monkeypatch, live):
    """The raw People resource is six nested lists; the question is 'who, email, role, phone'."""
    _install(monkeypatch, _FakePeople([_MARTA]))
    result = d.directory_search("marta")
    assert result["count"] == 1
    assert result["status"] == "searched"
    assert result["people"][0] == {
        "name": "Marta Kowal",
        "email": "marta@blanklabel.team",
        "title": "Head of Finance",
        "department": "Finance",
        "phone": "+385 91 000 0000",
        "photo_url": "https://lh3.example/photo",
        "resource_name": "people/c1",
    }


def test_primary_email_wins_over_the_first_listed(monkeypatch, live):
    """Google lists aliases first sometimes; the primary is the one to write to."""
    _install(monkeypatch, _FakePeople([_MARTA]))
    assert d.directory_search("marta")["people"][0]["email"] == "marta@blanklabel.team"


def test_missing_fields_become_none_not_a_crash(monkeypatch, live):
    """A directory entry with no phone or org is normal; a KeyError here hides everyone."""
    _install(monkeypatch, _FakePeople([{"resourceName": "people/c2",
                                        "names": [{"displayName": "Solo"}]}]))
    person = d.directory_search("solo")["people"][0]
    assert person["name"] == "Solo"
    assert person["email"] is None
    assert person["title"] is None
    assert person["department"] is None
    assert person["phone"] is None
    assert person["photo_url"] is None


def test_default_photo_is_not_reported(monkeypatch, live):
    """Google's silhouette placeholder is not a photo of the person."""
    _install(monkeypatch, _FakePeople([{
        "names": [{"displayName": "No Photo"}],
        "photos": [{"url": "https://lh3.example/default", "default": True}],
    }]))
    assert d.directory_search("no")["people"][0]["photo_url"] is None


def test_no_results_is_an_empty_list_with_count_zero(monkeypatch, live):
    """'people' absent from the response must not raise; nobody matching is an answer."""
    fake = _install(monkeypatch, _FakePeople())
    fake._people = None  # simulate the key being absent entirely

    class _NoKey(_FakePeople):
        def searchDirectoryPeople(self, **kwargs):  # noqa: N802
            self.calls.append(kwargs)
            return _Exec({})

    _install(monkeypatch, _NoKey())
    result = d.directory_search("nobody")
    assert result["people"] == []
    assert result["count"] == 0


def _http_error(status: int, message: str):
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status
            self.reason = "Error"

    body = json.dumps({"error": {"message": message}}).encode()
    return HttpError(_Resp(status), body)


def test_a_403_names_the_account_instead_of_a_stack_trace(monkeypatch, live):
    """A bare HttpError does not say which Workspace refused; the alias is the fix."""
    _install(monkeypatch, _FakePeople(raise_exc=_http_error(403, "permission denied")))
    with pytest.raises(RuntimeError, match="blt.*Workspace directory|Workspace directory.*blt"):
        d.directory_search("marta")
