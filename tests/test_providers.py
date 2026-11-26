"""Email provider names: from the domain, from the MX records, and rechecking a group."""

import pytest

from app.models import EmailProvider as P
from app.models import Role
from app.services import providers


@pytest.mark.parametrize(
    ("hosts", "domain", "expected"),
    [
        (["aspmx.l.google.com"], "acme.com", P.GOOGLE),
        (["acme-com.mail.protection.outlook.com"], "acme.com", P.MICROSOFT),
        (["mx.zoho.eu", "mx2.zoho.eu"], "acme.com", P.ZOHO),
        (["mta5.am0.yahoodns.net"], "acme.com", P.YAHOO),
        (["mx01.mail.icloud.com"], "acme.com", P.ICLOUD),
        (["mail.protonmail.ch"], "acme.com", P.PROTON),
        (["smtp.secureserver.net"], "acme.com", P.GODADDY),
        (["mx1.titan.email"], "acme.com", P.TITAN),
        (["mx.yandex.net"], "acme.com", P.YANDEX),
        (["inbound-smtp.us-east-1.amazonaws.com"], "acme.com", P.AMAZON),
        (["eu-smtp-inbound-1.mimecast.com"], "acme.com", P.MIMECAST),
        (["mx0a-001.pphosted.com"], "acme.com", P.PROOFPOINT),
        (["mail.acme.com"], "acme.com", P.OWN_SERVER),
        (["acme.com"], "acme.com", P.OWN_SERVER),
        (["mx.someisp.net"], "acme.com", P.OTHER),
        # A look-alike ending isn't the provider.
        (["mx.notgoogle.com"], "acme.com", P.OTHER),
        (["mail.notacme.com"], "acme.com", P.OTHER),
        ([], "acme.com", P.UNKNOWN),
    ],
)
def test_classify_by_mail_server(hosts, domain, expected):
    assert providers.classify(hosts, domain) == expected


def test_free_mail_domains_need_no_lookup(monkeypatch):
    def no_lookup(domain):
        raise AssertionError(f"looked up {domain}")

    monkeypatch.setattr(providers, "mx_hosts", no_lookup)
    assert providers.detect("Yahoo.co.uk") == P.YAHOO
    assert providers.detect("aol.com") == P.YAHOO
    assert providers.detect("icloud.com") == P.ICLOUD
    assert providers.detect("proton.me") == P.PROTON
    assert providers.detect("zohomail.com") == P.ZOHO
    assert providers.detect("hotmail.fr") == P.MICROSOFT


def test_import_names_own_server(client, admin):
    rows = [{"email": "a@acme-ownmail.com"}, {"email": "b@nomail.com"}, {"email": "c@me.com"}]
    r = client.post("/api/groups/import", json={"name": "Mix", "rows": rows})
    assert r.json()["provider_mix"] == {"OWN_SERVER": 1, "UNKNOWN": 1, "ICLOUD": 1}


def test_recheck_updates_old_leads(client, admin, db, monkeypatch, make_user, login):
    rows = [
        {"email": "a@zoho-co.com", "custom": {"industry": "SaaS", "job_title": "CTO"}},
        {"email": "b@zoho-co.com", "custom": {"website": "https://zoho-co.com"}},
        {"email": "c@acme-google.com"},
    ]
    gid = client.post("/api/groups/import", json={"name": "Old", "rows": rows}).json()["group"]["id"]
    facets = client.get(f"/api/groups/{gid}/facets").json()
    assert facets["custom_keys"] == ["industry", "job_title", "website"]
    assert {f["value"]: f["count"] for f in facets["providers"]} == {"OTHER": 2, "GOOGLE": 1}

    # zoho-co.com moved its email to Zoho since the import.
    real = providers.mx_hosts
    monkeypatch.setattr(providers, "mx_hosts", lambda d: ["mx.zoho.com"] if "zoho" in d else real(d))
    r = client.post(f"/api/groups/{gid}/recheck-providers")
    assert r.status_code == 200, r.text
    assert r.json() == {"checked": 3, "changed": 2, "provider_mix": {"ZOHO": 2, "GOOGLE": 1}}
    leads = client.get(f"/api/groups/{gid}/leads", params={"provider": "ZOHO"}).json()
    assert {lead["email"] for lead in leads["items"]} == {"a@zoho-co.com", "b@zoho-co.com"}

    logs = client.get("/api/logs", params={"action": "group.providers_rechecked"}).json()
    assert logs["items"][0]["after"] == {"checked": 3, "changed": 2}

    make_user("viewer@fastnexa.com", role=Role.VIEWER)
    login("viewer@fastnexa.com")
    assert client.post(f"/api/groups/{gid}/recheck-providers").status_code == 403
    assert client.post("/api/groups/999999/recheck-providers").status_code in (403, 404)
