import json
import re
import threading
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Tuple

from curl_cffi import requests


_ALLOCATED_LOCK = threading.Lock()
_ALLOCATED_ALIASES = set()
_CREATE_ALIAS_LOCK = threading.Lock()
_ACCOUNT_ROTATION_LOCK = threading.Lock()
_ACCOUNT_ROTATION_INDEX: Dict[str, int] = {}


class WwwAasaMailService:
    def __init__(
        self,
        api_url: str,
        api_token: str,
        username: str = "",
        password: str = "",
        gmail_account_id: str = "",
        gmail_account_ids: Any = None,
        alias_type: str = "domain",
        source: str = "OpenAI",
        wait_timeout: int = 20,
        create_on_demand: bool = True,
        cycle: bool = True,
        proxies: Optional[Dict[str, str]] = None,
        session: Any = None,
    ):
        self.api_url = str(api_url or "").strip().rstrip("/")
        self.api_token = str(api_token or "").strip()
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self.gmail_account_id = str(gmail_account_id or "").strip()
        self.gmail_account_ids = self._normalize_account_ids(gmail_account_ids, self.gmail_account_id)
        if self.gmail_account_ids and not self.gmail_account_id:
            self.gmail_account_id = self.gmail_account_ids[0]
        self.alias_type = self._normalize_alias_type(alias_type)
        self.source = str(source or "").strip()
        self.wait_timeout = max(3, min(30, int(wait_timeout or 20)))
        self.create_on_demand = bool(create_on_demand)
        self.cycle = bool(cycle)
        self.session = session or requests.Session(impersonate="chrome120")
        if proxies:
            self.session.proxies = proxies if isinstance(proxies, dict) else {"http": proxies, "https": proxies}

    @staticmethod
    def _normalize_alias_type(value: str) -> str:
        normalized = str(value or "domain").strip().lower()
        return normalized if normalized in {"plus", "dot", "domain", "all"} else "domain"

    @staticmethod
    def _normalize_account_ids(value: Any, fallback: str = "") -> List[str]:
        raw_items: List[Any]
        if isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        elif value is None:
            raw_items = []
        else:
            raw_items = re.split(r"[,;\s]+", str(value))

        account_ids: List[str] = []
        for item in raw_items:
            text = str(item or "").strip()
            if text and text not in account_ids:
                account_ids.append(text)

        fallback_text = str(fallback or "").strip()
        if not account_ids and fallback_text:
            account_ids.append(fallback_text)
        return account_ids

    def _rotation_key(self) -> str:
        return "|".join(
            [
                self.api_url,
                self.username,
                self.alias_type,
                ",".join(self.gmail_account_ids),
            ]
        )

    def _ordered_account_ids(self) -> List[str]:
        if not self.gmail_account_ids:
            return []
        key = self._rotation_key()
        with _ACCOUNT_ROTATION_LOCK:
            index = _ACCOUNT_ROTATION_INDEX.get(key, 0) % len(self.gmail_account_ids)
            _ACCOUNT_ROTATION_INDEX[key] = index + 1
        return self.gmail_account_ids[index:] + self.gmail_account_ids[:index]

    @staticmethod
    def _record_account_id(record: Dict[str, Any]) -> str:
        return str(record.get("gmail_account_id") or record.get("source_mailbox_id") or "").strip()

    @staticmethod
    def _record_id(record: Dict[str, Any]) -> int:
        try:
            return int(record.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _records_from_response(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        results = payload.get("results")
        if isinstance(results, list):
            return [item for item in results if isinstance(item, dict)]
        return []

    @staticmethod
    def token_from_state(state: Dict[str, Any]) -> str:
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def state_from_token(token: str) -> Dict[str, Any]:
        try:
            parsed = json.loads(token or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def extract_code_from_record(
        record: Dict[str, Any],
        extractor: Optional[Callable[[str], str]] = None,
    ) -> str:
        if not isinstance(record, dict):
            return ""

        direct_fields = ("code", "verification_code", "verify_code", "otp")
        for field in direct_fields:
            value = str(record.get(field) or "").strip()
            if re.fullmatch(r"\d{6}", value):
                return value

        content_parts = []
        for field in (
            "content",
            "verify_link",
            "subject",
            "title",
            "body",
            "text",
            "html",
            "raw",
            "source",
            "recipient",
            "alias_email",
        ):
            value = record.get(field)
            if value:
                content_parts.append(str(value))

        content = unescape(re.sub(r"<[^>]+>", " ", "\n".join(content_parts)))
        if extractor:
            code = extractor(content)
            if code:
                return code

        match = re.search(r"(?<!\d)(\d{6})(?!\d)", content)
        return match.group(1) if match else ""

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _get_json(self, path: str, params: Dict[str, Any], timeout: Optional[int] = None) -> Dict[str, Any]:
        if not self.api_url:
            return {}
        response = self.session.get(
            f"{self.api_url}{path}",
            params={k: v for k, v in params.items() if v not in (None, "")},
            headers=self._headers(),
            timeout=timeout or 15,
            verify=False,
        )
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return {"status": "error", "message": getattr(response, "text", "")}
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {"status": "error", "message": getattr(response, "text", "")}

    def get_aliases(self, limit: int = 200, order: str = "asc") -> List[Dict[str, Any]]:
        payload = self._get_json(
            "/api/get_aliases.php",
            {
                "status": "active",
                "alias_type": self.alias_type,
                "limit": max(1, min(200, int(limit or 200))),
                "order": order if order in {"asc", "desc"} else "asc",
            },
        )
        return self._records_from_response(payload)

    def get_codes(self, alias_email: str, limit: int = 10, auto_mark_read: bool = False) -> List[Dict[str, Any]]:
        params = {
            "alias_email": alias_email,
            "recipient": alias_email,
            "type": "all",
            "limit": max(1, min(100, int(limit or 10))),
            "auto_mark_read": "1" if auto_mark_read else "0",
        }
        if self.source:
            params["source"] = self.source
        payload = self._get_json("/api/get_codes.php", params)
        return self._records_from_response(payload)

    def latest_record_id(self, alias_email: str) -> int:
        records = self.get_codes(alias_email, limit=1, auto_mark_read=False)
        return max([self._record_id(record) for record in records] or [0])

    def _login_dashboard(self) -> bool:
        if not (self.username and self.password and self.api_url):
            return False
        response = self.session.post(
            f"{self.api_url}/login.php",
            data={"account": self.username, "password": self.password},
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.api_url,
                "Referer": f"{self.api_url}/login.php",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=20,
            verify=False,
        )
        text = str(getattr(response, "text", "") or "")
        return int(getattr(response, "status_code", 0) or 0) in {200, 302} and "login.php" not in text.lower()

    def disable_alias(self, alias_id: Any) -> Tuple[bool, str]:
        alias_id_text = str(alias_id or "").strip()
        if not alias_id_text:
            return False, "缺少 alias_id"
        if not (self.username and self.password):
            return False, "缺少 wwwaasa 账号密码，无法停用别名"
        if not self._login_dashboard():
            return False, "登录 wwwaasa 后台失败"

        response = self.session.post(
            f"{self.api_url}/dashboard.php",
            data={
                "action": "disable_alias",
                "alias_id": alias_id_text,
            },
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.api_url,
                "Referer": f"{self.api_url}/dashboard.php",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=20,
            verify=False,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code in {200, 302}:
            return True, "停用成功"
        return False, f"HTTP {status_code}"

    def disable_alias_from_token(self, state_token: str) -> Tuple[bool, str]:
        state = self.state_from_token(state_token)
        if state.get("provider") != "wwwaasa":
            return False, "非 wwwaasa token"
        return self.disable_alias(state.get("alias_id"))

    @staticmethod
    def _extract_aliases_from_html(html: str) -> List[str]:
        emails = re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", html or "")
        seen = set()
        aliases = []
        for email in emails:
            normalized = email.strip()
            if normalized and normalized.lower() not in seen:
                seen.add(normalized.lower())
                aliases.append(normalized)
        return aliases

    @staticmethod
    def _parse_source_mailboxes_from_html(html: str) -> List[Dict[str, str]]:
        resources: List[Dict[str, str]] = []
        seen = set()
        select_pattern = re.compile(r"<select\b([^>]*)>(.*?)</select>", re.I | re.S)
        target_selects = []
        for select_match in select_pattern.finditer(html or ""):
            attrs = select_match.group(1) or ""
            if re.search(r"\b(?:name|id)\s*=\s*(?:['\"]gmail_account_id['\"]|gmail_account_id)(?=\s|$|>|/)", attrs, re.I):
                target_selects.append(select_match.group(2) or "")
        search_html = "\n".join(target_selects) if target_selects else (html or "")

        option_pattern = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.I | re.S)
        value_pattern = re.compile(r"\bvalue\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))", re.I | re.S)
        type_pattern = re.compile(r"\bdata-mailbox-type\s*=\s*(?:(['\"])(.*?)\1|([^\s>]+))", re.I | re.S)
        for match in option_pattern.finditer(search_html):
            attrs = match.group(1) or ""
            label = unescape(re.sub(r"<[^>]+>", " ", match.group(2) or ""))
            label = re.sub(r"\s+", " ", label).strip()
            value_match = value_pattern.search(attrs)
            if not value_match:
                continue
            resource_id = (value_match.group(2) or value_match.group(3) or "").strip()
            if not resource_id or resource_id in seen:
                continue
            seen.add(resource_id)
            type_match = type_pattern.search(attrs)
            resource_type = ((type_match.group(2) or type_match.group(3) or "").strip().lower() if type_match else "")
            display_name = label.split("（", 1)[0].strip() or label
            resources.append(
                {
                    "id": resource_id,
                    "type": resource_type,
                    "name": display_name,
                    "label": label,
                }
            )
        return resources

    def get_source_mailboxes(self) -> Tuple[bool, Any]:
        if not self._login_dashboard():
            return False, "登录 wwwaasa 后台失败"
        response = self.session.get(
            f"{self.api_url}/dashboard.php",
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": f"{self.api_url}/dashboard.php",
                "User-Agent": "Mozilla/5.0",
            },
            timeout=20,
            verify=False,
        )
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            return False, f"HTTP {status_code}"
        resources = self._parse_source_mailboxes_from_html(str(getattr(response, "text", "") or ""))
        return True, resources

    def create_alias(self) -> Tuple[Optional[str], Dict[str, Any]]:
        if not (self.username and self.password and self.gmail_account_ids):
            return None, {}

        ordered_account_ids = self._ordered_account_ids()
        with _CREATE_ALIAS_LOCK:
            before = {str(item.get("alias_email") or "").lower() for item in self.get_aliases(limit=200, order="desc")}
            if not self._login_dashboard():
                return None, {}

            for account_id in ordered_account_ids:
                response = self.session.post(
                    f"{self.api_url}/dashboard.php",
                    data={
                        "action": "create_alias",
                        "gmail_account_id": account_id,
                        "alias_type": "domain" if self.alias_type == "all" else self.alias_type,
                        "quantity": "1",
                    },
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": self.api_url,
                        "Referer": f"{self.api_url}/dashboard.php",
                        "User-Agent": "Mozilla/5.0",
                    },
                    timeout=30,
                    verify=False,
                )
                html = str(getattr(response, "text", "") or "")
                if int(getattr(response, "status_code", 0) or 0) not in {200, 302}:
                    continue

                aliases = self.get_aliases(limit=20, order="desc")
                account_matches: List[Dict[str, Any]] = []
                loose_matches: List[Dict[str, Any]] = []
                for item in aliases:
                    email = str(item.get("alias_email") or "").strip()
                    if not email or email.lower() in before:
                        continue
                    item_account_id = self._record_account_id(item)
                    if item_account_id and item_account_id == account_id:
                        account_matches.append(item)
                    else:
                        loose_matches.append(item)

                for item in account_matches + loose_matches:
                    email = str(item.get("alias_email") or "").strip()
                    if email:
                        return email, item

                for email in self._extract_aliases_from_html(html):
                    if email.lower() not in before:
                        return email, {"alias_email": email, "gmail_account_id": account_id}
        return None, {}

    def allocate_email(self) -> Tuple[Optional[str], Optional[str]]:
        item: Dict[str, Any] = {}
        email = None
        if self.create_on_demand:
            email, item = self.create_alias()

        if not email:
            aliases = self.get_aliases(limit=200, order="asc")
            with _ALLOCATED_LOCK:
                account_ids = self._ordered_account_ids()
                ordered_aliases: List[Dict[str, Any]] = []
                if account_ids:
                    for account_id in account_ids:
                        ordered_aliases.extend(
                            candidate
                            for candidate in aliases
                            if self._record_account_id(candidate) in {"", account_id}
                        )
                else:
                    ordered_aliases = aliases

                seen_emails = set()
                for candidate in ordered_aliases:
                    candidate_email = str(candidate.get("alias_email") or "").strip()
                    candidate_key = candidate_email.lower()
                    if not candidate_email or candidate_key in seen_emails:
                        continue
                    seen_emails.add(candidate_key)
                    if candidate_key not in _ALLOCATED_ALIASES:
                        _ALLOCATED_ALIASES.add(candidate_key)
                        email = candidate_email
                        item = candidate
                        break
                if not email and self.cycle and aliases:
                    item = ordered_aliases[0] if ordered_aliases else aliases[0]
                    email = str(item.get("alias_email") or "").strip()
                    if email:
                        _ALLOCATED_ALIASES.add(email.lower())
        else:
            with _ALLOCATED_LOCK:
                _ALLOCATED_ALIASES.add(email.lower())

        if not email:
            return None, None

        state = {
            "provider": "wwwaasa",
            "alias_email": email,
            "alias_id": item.get("id"),
            "alias_type": item.get("alias_type") or self.alias_type,
            "gmail_account_id": item.get("gmail_account_id") or self.gmail_account_id,
            "last_record_id": self.latest_record_id(email),
        }
        return email, self.token_from_state(state)

    def wait_for_code(
        self,
        alias_email: str,
        state_token: str = "",
        extractor: Optional[Callable[[str], str]] = None,
    ) -> str:
        state = self.state_from_token(state_token)
        after_id = self._record_id({"id": state.get("last_record_id")})
        params = {
            "after_id": after_id or "",
            "timeout": self.wait_timeout,
            "alias_email": alias_email,
            "recipient": alias_email,
            "type": "code",
            "auto_mark_read": "1",
        }
        if self.source:
            params["source"] = self.source

        payload = self._get_json("/api/wait_code.php", params, timeout=self.wait_timeout + 5)
        records = self._records_from_response(payload)
        if not records:
            records = [
                record for record in self.get_codes(alias_email, limit=10, auto_mark_read=True)
                if self._record_id(record) > after_id
            ]

        for record in sorted(records, key=self._record_id, reverse=True):
            code = self.extract_code_from_record(record, extractor=extractor)
            if code:
                return code
        return ""
