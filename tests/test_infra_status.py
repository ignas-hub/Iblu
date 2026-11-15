"""get_infra_status: a host that reported minutes ago must not be called stale.

Found 2026-11-08: three of five hosts' collectors write `timestamp`, two write
`ts`. The status read only `ts`, so appsprod, appsdev and the n8n VM showed as
"stale" in every report while reporting every few minutes.
"""

from datetime import datetime, timedelta, timezone

from dataclasses import replace

from iblu_keeper.tools import infra


def _settings(monkeypatch, **overrides):
    """Settings is a frozen dataclass: swap in a copy instead of assigning."""
    monkeypatch.setattr(infra, "settings", replace(infra.settings, **overrides))


def _hosts(**stamps):
    return {"hosts": [{"host": name, **stamp} for name, stamp in stamps.items()]}


def test_both_timestamp_spellings_count_as_fresh():
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fn = infra._derive_status
    status, summary = fn(_hosts(hub={"ts": now}, appsprod={"timestamp": now}))
    assert "2/2 hosts healthy" in summary, summary


def test_an_old_report_is_still_stale_under_either_spelling():
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat().replace("+00:00", "Z")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    fn = infra._derive_status
    _status, summary = fn(_hosts(hub={"ts": now}, appsprod={"timestamp": old}))
    assert "1 stale (appsprod)" in summary, summary


# --------------------------------------------------------------------------- #
# DigitalOcean section (added 2026-11-15): read-only, independent of Drive
# --------------------------------------------------------------------------- #
_DO_PAYLOADS = {
    "/account": {"account": {"email": "ops@example.com", "status": "active",
                             "droplet_limit": 25, "team": {"name": "Deadlift"}}},
    "/customers/my/balance": {"month_to_date_usage": "48.12",
                              "month_to_date_balance": "48.12",
                              "account_balance": "0.00",
                              "generated_at": "2026-11-15T12:00:00Z"},
    "/droplets": {"droplets": [
        {"name": "web-1", "status": "active", "size_slug": "s-2vcpu-4gb",
         "region": {"slug": "fra1"}, "tags": ["prod"], "created_at": "2025-01-01T00:00:00Z",
         "image": {"distribution": "Ubuntu", "name": "24.04 LTS"},
         "networks": {"v4": [{"type": "public", "ip_address": "203.0.113.5"},
                             {"type": "private", "ip_address": "10.0.0.5"}]}},
        {"name": "worker-1", "status": "off", "size_slug": "s-1vcpu-1gb",
         "region": {"slug": "fra1"}, "networks": {"v4": []}},
    ]},
    "/apps": {"apps": [{"spec": {"name": "landing"}, "live_url": "https://landing.example",
                        "region": {"slug": "fra"},
                        "active_deployment": {"phase": "ACTIVE"}}]},
    "/databases": {"databases": [{"name": "pg-main", "engine": "pg", "version": "16",
                                  "status": "online", "size": "db-s-1vcpu-1gb",
                                  "region": "fra1", "num_nodes": 1}]},
    "/domains": {"domains": [{"name": "example.com"}]},
    "/volumes": {"volumes": []},
    "/load_balancers": {"load_balancers": []},
    "/kubernetes/clusters": {"kubernetes_clusters": []},
}


def test_digitalocean_snapshot_flattens_and_flags_stopped_droplet(monkeypatch):
    calls = []

    def fake_get(token, path):
        assert token == "dop_v1_test"
        calls.append(path)
        return _DO_PAYLOADS[path]

    monkeypatch.setattr(infra, "_do_get", fake_get)
    snap = infra._digitalocean_snapshot("dop_v1_test")
    assert snap["status"] == "ok", snap
    assert snap["account"]["email"] == "ops@example.com"
    assert snap["account"]["team"] == "Deadlift"
    assert snap["balance"]["month_to_date_usage_usd"] == "48.12"
    web = next(d for d in snap["droplets"] if d["name"] == "web-1")
    assert web["public_ip"] == "203.0.113.5" and web["private_ip"] == "10.0.0.5"
    assert web["image"] == "Ubuntu 24.04 LTS"
    assert snap["notes"] == ["droplet worker-1 is off"]
    assert "2 droplets" in snap["summary"] and "1 apps" in snap["summary"]
    assert "worker-1 is off" in snap["summary"]
    assert len(calls) == 9 and len(set(calls)) == 9


def test_digitalocean_one_failing_endpoint_keeps_the_rest(monkeypatch):
    def fake_get(token, path):
        if path == "/kubernetes/clusters":
            raise RuntimeError("DigitalOcean token lacks read scope for /kubernetes/clusters")
        return _DO_PAYLOADS[path]

    monkeypatch.setattr(infra, "_do_get", fake_get)
    snap = infra._digitalocean_snapshot("t")
    assert snap["status"] == "error"
    assert snap["kubernetes_clusters"] is None
    assert len(snap["droplets"]) == 2  # the rest still came back
    assert any("kubernetes_clusters" in e for e in snap["errors"])


def test_digitalocean_without_token_is_not_configured_and_never_calls_out(monkeypatch):
    _settings(monkeypatch, digitalocean_token="")

    def boom(*a, **k):
        raise AssertionError("must not call the DO API without a token")

    monkeypatch.setattr(infra, "_do_get", boom)
    assert infra._digitalocean()["status"] == "not_configured"


def test_get_infra_status_carries_digitalocean_even_when_drive_fails(monkeypatch):
    infra._CACHE.clear()
    _settings(monkeypatch, infra_folder_id="folder123", digitalocean_token="t")
    monkeypatch.setattr(infra, "_do_get", lambda token, path: _DO_PAYLOADS[path])

    def no_drive():
        raise RuntimeError("Drive down")

    monkeypatch.setattr(infra, "_drive", no_drive)
    res = infra.get_infra_status()
    assert res["error"].startswith("Drive fetch failed")
    assert res["digitalocean"]["status"] == "ok"
    assert len(res["digitalocean"]["droplets"]) == 2
    infra._CACHE.clear()


def test_get_infra_status_degrades_on_a_stopped_droplet(monkeypatch):
    infra._CACHE.clear()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    latest = {"generated": now, "hosts": [{"host": "hub", "ts": now}]}
    _settings(monkeypatch, infra_folder_id="folder123", digitalocean_token="t")
    monkeypatch.setattr(infra, "_do_get", lambda token, path: _DO_PAYLOADS[path])
    monkeypatch.setattr(infra, "_drive", lambda: object())

    import json

    def fake_fetch(drive, folder, name):
        return (b"# ok" if name == "latest.md" else json.dumps(latest).encode())

    monkeypatch.setattr(infra, "_fetch_file_bytes", fake_fetch)
    res = infra.get_infra_status()
    assert res["status"] == "degraded", res["summary"]
    assert "worker-1 is off" in res["summary"]
    assert res["digitalocean"]["account"]["email"] == "ops@example.com"
    infra._CACHE.clear()


def test_digitalocean_balance_without_billing_scope_is_not_an_error(monkeypatch):
    def fake_get(token, path):
        if path == "/customers/my/balance":
            raise RuntimeError("DigitalOcean token lacks read scope for /customers/my/balance")
        return _DO_PAYLOADS[path]

    monkeypatch.setattr(infra, "_do_get", fake_get)
    snap = infra._digitalocean_snapshot("t")
    assert snap["status"] == "ok", snap["errors"]
    assert "billing:read" in snap["balance"]["unavailable"]
