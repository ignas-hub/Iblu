"""Tests for `gdoc_read`'s `structure` parameter (tools.gmail.read_gdoc).

Default `structure=False` must keep the tool's output byte-for-byte
unchanged; `structure=True` adds a `structure` field derived from the Docs
API, or `None` + a note for non-Doc files. Fakes only, no network.
"""

from __future__ import annotations

import iblu_keeper.tools.gmail as gmail_real


def test_structure_false_is_the_original_mock_shape():
    result = gmail_real.read_gdoc("some-id")  # structure defaults to False
    assert result == {
        "file_id": "some-id", "name": "Mock Doc", "text": "Mock document content.",
        "truncated": False, "mock": True,
    }
    assert "structure" not in result


def test_structure_true_adds_structure_field_in_mock_mode():
    result = gmail_real.read_gdoc("some-id", structure=True)
    assert result["structure"] == []
    # everything else stays exactly as before
    without_structure = {k: v for k, v in result.items() if k != "structure"}
    assert without_structure == {
        "file_id": "some-id", "name": "Mock Doc", "text": "Mock document content.",
        "truncated": False, "mock": True,
    }


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeDriveService:
    def __init__(self, meta, export_text):
        self._meta = meta
        self._export_text = export_text

    def files(self):
        return self

    def get(self, **kwargs):
        return _Exec(self._meta)

    def export(self, **kwargs):
        return _Exec(self._export_text)


class _FakeDocsService:
    def __init__(self, doc):
        self._doc = doc

    def documents(self):
        return self

    def get(self, **kwargs):
        return _Exec(self._doc)


FAKE_DOC = {
    "body": {"content": [
        {"startIndex": 1, "endIndex": 6, "paragraph": {
            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
            "elements": [{"startIndex": 1, "endIndex": 6, "textRun": {"content": "Hi\n"}}],
        }},
    ]}
}


class _Live:
    use_mock = False


def test_structure_true_on_a_live_google_doc(monkeypatch):
    import iblu_keeper.google_auth as google_auth

    monkeypatch.setattr(gmail_real, "settings", _Live())

    drive_meta = {"id": "DOC1", "name": "Real Doc", "mimeType": "application/vnd.google-apps.document"}
    fake_drive = _FakeDriveService(drive_meta, "Hi\n")
    fake_docs = _FakeDocsService(FAKE_DOC)

    def fake_build_service(api, version, scopes=None, account=None):
        return fake_drive if api == "drive" else fake_docs

    monkeypatch.setattr(google_auth, "build_service", fake_build_service)

    result = gmail_real.read_gdoc("DOC1", structure=True)
    assert result["text"] == "Hi\n"
    assert result["structure"][0]["type"] == "paragraph"
    assert result["structure"][0]["text_runs"][0]["text"] == "Hi\n"


def test_structure_true_on_a_spreadsheet_gets_none_and_a_note(monkeypatch):
    import iblu_keeper.google_auth as google_auth

    monkeypatch.setattr(gmail_real, "settings", _Live())

    drive_meta = {"id": "SS1", "name": "A Sheet", "mimeType": "application/vnd.google-apps.spreadsheet"}
    fake_drive = _FakeDriveService(drive_meta, "a,b\n1,2\n")

    def fake_build_service(api, version, scopes=None, account=None):
        return fake_drive

    monkeypatch.setattr(google_auth, "build_service", fake_build_service)

    result = gmail_real.read_gdoc("SS1", structure=True)
    assert result["structure"] is None
    assert "structure_note" in result
