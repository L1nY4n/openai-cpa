import json
import unittest

from utils.email_providers.wwwaasa_service import WwwAasaMailService
from utils.email_providers import wwwaasa_service


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.created_records = []
        self.disabled_ids = []
        self.posts = []
        self.gets = []

    def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        if url.endswith("/api/get_aliases.php"):
            data = list(self.created_records)
            if (params or {}).get("order") == "desc":
                data = list(reversed(data))
            return _Response(payload={"status": "success", "data": data})
        if url.endswith("/api/get_codes.php"):
            return _Response(payload={"status": "success", "data": []})
        if url.endswith("/api/wait_code.php"):
            return _Response(
                payload={
                    "status": "success",
                    "data": {
                        "id": 188,
                        "content": "Your OpenAI code is 123456",
                        "alias_email": "goou98id11@mydome004.fun",
                    },
                }
            )
        if url.endswith("/dashboard.php"):
            return _Response(
                text="""
                <select name="alias_type">
                    <option value="domain">domain</option>
                </select>
                <select name="gmail_account_id">
                    <option value="12" data-mailbox-type="gmail">managed-gmail@example.com（Gmail 托管 / 平台共享 / 新增将占用名额）</option>
                    <option value="10" data-mailbox-type="domain">hosted-one.example（域名托管 / 平台域名 / 不占主邮箱配额）</option>
                    <option value=9 data-mailbox-type=domain>hosted-two.example（域名托管 / 平台域名 / 不占主邮箱配额）</option>
                </select>
                """
            )
        return _Response(status_code=404, payload={"status": "error"})

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data or {}))
        if url.endswith("/login.php"):
            return _Response(text="<html>dashboard</html>")
        if url.endswith("/dashboard.php"):
            if (data or {}).get("action") == "disable_alias":
                self.disabled_ids.append(str((data or {}).get("alias_id") or ""))
                return _Response(text="<html>disabled</html>")
            account_id = str((data or {}).get("gmail_account_id") or "10")
            domains = {"10": "hosted-one.example", "9": "hosted-two.example"}
            aliases = {
                "10": "alias-one@hosted-one.example",
                "9": "alias-two@hosted-two.example",
            }
            email = aliases.get(account_id, f"mail{len(self.created_records) + 1}@{domains.get(account_id, 'example.com')}")
            self.created_records.append(
                {
                    "id": 187 + len(self.created_records),
                    "alias_email": email,
                    "alias_type": "domain",
                    "gmail_account_id": int(account_id) if account_id.isdigit() else account_id,
                }
            )
            return _Response(text=f'成功生成 1 个临时邮箱 <td>{email}</td>')
        return _Response(status_code=404)


class WwwAasaMailServiceTests(unittest.TestCase):
    def setUp(self):
        wwwaasa_service._ALLOCATED_ALIASES.clear()
        wwwaasa_service._ACCOUNT_ROTATION_INDEX.clear()

    def test_allocate_email_creates_alias_and_returns_state_token(self):
        service = WwwAasaMailService(
            api_url="https://mail.wwwaasa.top",
            api_token="test-token",
            username="test-user",
            password="test-password",
            gmail_account_id="10",
            alias_type="domain",
            session=_FakeSession(),
        )

        email, token = service.allocate_email()

        self.assertEqual("alias-one@hosted-one.example", email)
        state = json.loads(token)
        self.assertEqual("wwwaasa", state["provider"])
        self.assertEqual("domain", state["alias_type"])
        self.assertEqual(187, state["alias_id"])
        self.assertEqual(10, state["gmail_account_id"])

    def test_allocate_email_rotates_configured_account_ids(self):
        session = _FakeSession()
        service = WwwAasaMailService(
            api_url="https://mail.wwwaasa.top",
            api_token="test-token",
            username="test-user",
            password="test-password",
            gmail_account_ids="10,9",
            alias_type="domain",
            session=session,
        )

        first_email, first_token = service.allocate_email()
        second_email, second_token = service.allocate_email()

        self.assertEqual("alias-one@hosted-one.example", first_email)
        self.assertEqual("alias-two@hosted-two.example", second_email)
        self.assertEqual("10", session.posts[1][1]["gmail_account_id"])
        self.assertEqual("9", session.posts[3][1]["gmail_account_id"])
        self.assertEqual(10, json.loads(first_token)["gmail_account_id"])
        self.assertEqual(9, json.loads(second_token)["gmail_account_id"])

    def test_disable_alias_from_token_posts_dashboard_action(self):
        session = _FakeSession()
        service = WwwAasaMailService(
            api_url="https://mail.wwwaasa.top",
            api_token="test-token",
            username="test-user",
            password="test-password",
            session=session,
        )
        token = json.dumps({"provider": "wwwaasa", "alias_id": 209})

        ok, msg = service.disable_alias_from_token(token)

        self.assertTrue(ok, msg)
        self.assertEqual(["209"], session.disabled_ids)
        self.assertEqual("disable_alias", session.posts[-1][1]["action"])
        self.assertEqual("209", session.posts[-1][1]["alias_id"])

    def test_get_source_mailboxes_parses_dashboard_options(self):
        service = WwwAasaMailService(
            api_url="https://mail.wwwaasa.top",
            api_token="test-token",
            username="test-user",
            password="test-password",
            session=_FakeSession(),
        )

        ok, resources = service.get_source_mailboxes()

        self.assertTrue(ok, resources)
        self.assertEqual(["12", "10", "9"], [item["id"] for item in resources])
        self.assertEqual("domain", resources[1]["type"])
        self.assertEqual("hosted-one.example", resources[1]["name"])

    def test_wait_for_code_extracts_code_from_wait_response(self):
        service = WwwAasaMailService(
            api_url="https://mail.wwwaasa.top",
            api_token="test-token",
            source="OpenAI",
            session=_FakeSession(),
        )

        code = service.wait_for_code(
            "alias-one@hosted-one.example",
            json.dumps({"last_record_id": 187}),
        )

        self.assertEqual("123456", code)

    def test_extract_code_from_record_supports_content_field(self):
        code = WwwAasaMailService.extract_code_from_record(
            {"content": "verification code to continue: 654321"}
        )

        self.assertEqual("654321", code)


if __name__ == "__main__":
    unittest.main()
