"""OdooClient web deep-link builders + Odoo major-version detection."""
import asyncio

from cogs.timetracking.odoo.client import OdooClient

API_URL = "https://x.odoo.com/json/2"   # ODOO_URL is the JSON-2 API base


def _client(url=API_URL):
    # db/username/api_key present so .loaded is True (not needed for pure URL builders)
    return OdooClient(url, "db", "user", "key")


def test_web_root_strips_api_path():
    assert _client().web_root == "https://x.odoo.com"


def test_fs_task_url():
    assert _client().fs_task_url(5) == "https://x.odoo.com/odoo/field-service/5"


def test_project_url_uses_action_578():
    assert _client().project_url(29) == "https://x.odoo.com/odoo/action-578/29/project-timesheets"


def test_partner_url_contacts_path():
    assert _client().partner_url(9) == "https://x.odoo.com/odoo/contacts/9"


def test_builders_none_when_url_or_id_missing():
    no_url = OdooClient(None, "db", "user", "key")
    assert no_url.web_root is None
    assert no_url.fs_task_url(5) is None
    assert no_url.project_url(29) is None
    assert no_url.partner_url(9) is None
    c = _client()
    assert c.fs_task_url(None) is None
    assert c.project_url(0) is None
    assert c.partner_url(None) is None


def test_detect_version_parses_major():
    c = _client()

    async def fake_call(endpoint, data):
        assert endpoint == "/ir.module.module/search_read"
        assert ["name", "=", "base"] in data["domain"]
        return [{"latest_version": "19.0.1.3"}]

    c.call = fake_call
    assert asyncio.run(c.detect_version()) == 19
    assert c.odoo_version == 19


def test_detect_version_tolerates_junk():
    c = _client()

    async def empty(endpoint, data):
        return []

    c.call = empty
    assert asyncio.run(c.detect_version()) is None   # no row -> stays None, no raise

    async def junk(endpoint, data):
        return [{"latest_version": "saas~19"}]       # non-leading-digit -> ignored

    c.call = junk
    assert asyncio.run(c.detect_version()) is None

    async def boom(endpoint, data):
        raise RuntimeError("network")

    c.call = boom
    assert asyncio.run(c.detect_version()) is None   # swallowed
