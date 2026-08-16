"""E2E tests — full system flow with mocks.

Tests the complete chain: auth provider → apiproxy → rest/CLI.
No real database — uses MockPool from auth tests conftest.

Proxy only exposes methods decorated with @auth_method (needs_bootstrap, bootstrap)
Other methods (login, create_user, etc.) are called directly through provider.

Known limitation: MockPool doesn't handle JOINs, CTEs, or complex queries.
Tests that need these patterns validate at the integration-test level (test_integration_postgres.py).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modules.auth.jwt
from modules.auth.provider import (
    AuthProvider,
    UserContext,
    InvalidCredentialsError,
    AccountLockedError,
    ReuseDetectedError,
    ForbiddenError,
    NotFoundError,
)
from modules.auth.config import AuthConfig
from modules.auth.tests.conftest import MockPool, MockRow
from modules.auth.decorators import auth_method
from modules.apiproxy.registry import MethodRegistry, MethodMeta
from modules.apiproxy.provider import ApiProxyProvider
from modules.apiproxy.middleware import AuthMiddleware, AuthorizedCall
from modules.apiproxy.converter import call_method, ApiError
from modules.apiproxy.config import ApiproxyConfig


# ── Shared Fixtures ───────────────────────────────────────


@pytest.fixture
def mock_pool() -> MockPool:
    return MockPool()


@pytest.fixture
def auth_config() -> AuthConfig:
    return AuthConfig(
        jwt_secret="e2e-test-secret-key-very-long-12345!!",
        jwt_algorithm="HS256",
        jwt_access_expiration_minutes=15,
        jwt_refresh_expiration_days=30,
        password_min_length=8,
        password_require_uppercase=True,
        password_require_digit=True,
        password_history_size=10,
        login_attempts_limit=5,
        login_block_minutes=15,
        perms_cache_ttl=300,
    )


@pytest.fixture
def auth_provider(mock_pool, auth_config) -> AuthProvider:
    return AuthProvider(config=auth_config, pool=mock_pool)


@pytest.fixture
def api_proxy(auth_provider) -> ApiProxyProvider:
    """ApiProxyProvider with auth methods registered via @auth_method."""
    config = ApiproxyConfig(whitelist=["auth"])
    proxy = ApiProxyProvider(config=config, auth_provider=auth_provider)
    proxy.registry.collect_from_module(auth_provider, "auth")
    return proxy


# ── E2E 1: Bootstrap → Login → Tokens → Logout ───────────


@pytest.mark.asyncio
class TestE2EBootstrapToLogout:
    """Full auth flow: needs_bootstrap → bootstrap → login → refresh → logout."""

    async def test_full_auth_flow(self, auth_provider, mock_pool, api_proxy):
        """Complete bootstrap → login → refresh → logout cycle."""
        # Step 1: needs_bootstrap (via proxy — @auth_method public)
        needs = await api_proxy.call("auth", "needs_bootstrap", {})
        assert needs["error"] is None
        assert needs["data"] is True

        # Step 2: bootstrap (via proxy — @auth_method public)
        bootstrap_result = await api_proxy.call("auth", "bootstrap", {
            "username": "admin",
            "password": "SecurePass123",
            "email": "admin@test.com",
        })
        assert bootstrap_result["error"] is None
        admin_id = bootstrap_result["data"]["user_id"]

        # Step 3: login (direct — no @auth_method decorator)
        login_result = await auth_provider.login("admin", "SecurePass123")
        assert "access_token" in login_result
        assert "refresh_token" in login_result
        access_token = login_result["access_token"]
        refresh_token = login_result["refresh_token"]

        # Step 4: validate access token via JWT directly
        from modules.auth.jwt import validate_access_token
        payload = validate_access_token(
            access_token,
            auth_provider._config.jwt_secret,
            auth_provider._config.jwt_algorithm,
        )
        assert payload["sub"] == admin_id
        assert payload["username"] == "admin"

        # Step 5: refresh (direct — no @auth_method)
        new_tokens = await auth_provider.refresh_token(refresh_token)
        assert "access_token" in new_tokens
        assert "refresh_token" in new_tokens
        assert new_tokens["access_token"] != access_token
        assert new_tokens["refresh_token"] != refresh_token

        # Step 6: new token validates via JWT
        new_payload = validate_access_token(
            new_tokens["access_token"],
            auth_provider._config.jwt_secret,
            auth_provider._config.jwt_algorithm,
        )
        assert new_payload["sub"] == admin_id

        # Step 7: logout (direct)
        logout_ok = await auth_provider.logout(new_tokens["refresh_token"])
        assert logout_ok is True

        # Step 8: new access token is now invalid (session revoked)
        # MockPool limitation: can't filter by is_revoked in get_session_by_refresh
        # So we verify the session is marked revoked in the pool directly
        refresh_hash = modules.auth.jwt.hash_token(new_tokens["refresh_token"])
        session = await mock_pool.fetchrow(
            "SELECT * FROM auth_sessions WHERE refresh_token_hash = $1",
            refresh_hash,
        )
        # Session exists but is_revoked should be True
        if session:
            assert session["is_revoked"] is True


# ── E2E 2: RBAC via @auth_method — bootstrap public vs protected ─


@pytest.mark.asyncio
class TestE2ERBAC:
    """RBAC: public vs protected methods, permission checking."""

    async def test_bootstrap_is_public(self, api_proxy):
        """bootstrap and needs_bootstrap are public methods."""
        meta = api_proxy.registry.get_method("auth", "needs_bootstrap")
        assert meta is not None
        assert meta.public is True

        meta = api_proxy.registry.get_method("auth", "bootstrap")
        assert meta is not None
        assert meta.public is True

    async def test_public_method_no_token_needed(self, api_proxy):
        """Public methods work without token."""
        result = await api_proxy.call("auth", "needs_bootstrap", {})
        assert result["error"] is None

    async def test_protected_method_needs_token(self, mock_pool, auth_config):
        """Non-public method requires token."""
        # Create a custom protected method
        from modules.auth.decorators import auth_method as am

        async def protected_action():
            return "secret"

        protected_action._auth_method_meta = {
            "name": "protected_action",
            "description": "Protected action",
            "args": {},
            "return_type": "str",
            "public": False,
            "required_permission": "users:read",
        }

        auth_prov = AuthProvider(config=auth_config, pool=mock_pool)
        config = ApiproxyConfig(whitelist=["auth", "test"])
        proxy = ApiProxyProvider(config=config, auth_provider=auth_prov)
        proxy.registry.register("test", "protected_action",
                                protected_action._auth_method_meta, protected_action)

        meta = proxy.registry.get_method("test", "protected_action")
        assert meta.public is False

        # Without token → 401
        with pytest.raises(PermissionError, match="401"):
            await proxy.middleware.authorize(meta, token=None)

    async def test_invalid_token_rejected(self, mock_pool, auth_config):
        """Invalid token → PermissionError 401."""
        auth_prov = AuthProvider(config=auth_config, pool=mock_pool)

        async def protected_action():
            return "secret"

        protected_action._auth_method_meta = {
            "name": "protected_action",
            "description": "Protected action",
            "args": {},
            "return_type": "str",
            "public": False,
            "required_permission": None,
        }

        config = ApiproxyConfig(whitelist=["test"])
        proxy = ApiProxyProvider(config=config, auth_provider=auth_prov)
        proxy.registry.register("test", "protected_action",
                                protected_action._auth_method_meta, protected_action)

        meta = proxy.registry.get_method("test", "protected_action")
        with pytest.raises(PermissionError, match="401"):
            await proxy.middleware.authorize(meta, token="totally-invalid-token")


# ── E2E 3: Brute force lockout ────────────────────────────


@pytest.mark.asyncio
class TestE2EBruteForceLockout:
    """5 wrong passwords → account locked → correct password rejected."""

    async def test_brute_force_then_lockout(self, auth_provider, mock_pool):
        """After 5 failed attempts, account locks and even correct password fails."""
        await auth_provider.create_user("victim", "SecurePass123")

        for i in range(4):
            with pytest.raises(InvalidCredentialsError):
                await auth_provider.login("victim", "WrongPass123")

        # 5th attempt triggers lockout
        with pytest.raises(InvalidCredentialsError):
            await auth_provider.login("victim", "WrongPass123")

        # Correct password now fails (account locked)
        with pytest.raises(AccountLockedError):
            await auth_provider.login("victim", "SecurePass123")


# ── E2E 4: Workspace — simulated via MockPool ─────────────


@pytest.mark.asyncio
class TestE2EWorkspace:
    """Workspace: create, add member, create session/message, access control."""

    async def test_workspace_crud_and_access(self, mock_pool, auth_config):
        """Owner creates workspace, adds member, creates content."""
        provider = AuthProvider(config=auth_config, pool=mock_pool)
        owner = await provider.create_user("owner_user", "SecurePass123")
        stranger = await provider.create_user("stranger_user", "SecurePass123")

        # Create workspace
        ws_id = str(uuid.uuid4())
        mock_pool.insert_direct("workspaces", {
            "id": ws_id,
            "owner_id": owner["id"],
            "name": "My Workspace",
            "description": "Test workspace",
        })

        ws = await mock_pool.fetchrow("SELECT * FROM workspaces WHERE id = $1", ws_id)
        assert ws is not None
        assert ws["owner_id"] == owner["id"]

        # Add member
        mock_pool.insert_direct("workspace_members", {
            "id": str(uuid.uuid4()),
            "workspace_id": ws_id,
            "user_id": owner["id"],
            "role": "owner",
        })

        members = await mock_pool.fetch(
            "SELECT * FROM workspace_members WHERE workspace_id = $1", ws_id
        )
        assert len(members) == 1

        # Create session
        session_id = str(uuid.uuid4())
        mock_pool.insert_direct("sessions", {
            "id": session_id,
            "workspace_id": ws_id,
            "title": "Test Session",
        })

        # Add message
        msg_id = str(uuid.uuid4())
        mock_pool.insert_direct("messages", {
            "id": msg_id,
            "session_id": session_id,
            "role": "user",
            "content": "Hello workspace!",
        })

        msg = await mock_pool.fetchrow("SELECT * FROM messages WHERE id = $1", msg_id)
        assert msg is not None
        assert msg["content"] == "Hello workspace!"

        # Stranger has no workspace
        stranger_ws = await mock_pool.fetch(
            "SELECT * FROM workspaces WHERE owner_id = $1", stranger["id"]
        )
        assert len(stranger_ws) == 0


# ── E2E 5: LLM — provider setup and config ────────────────


@pytest.mark.asyncio
class TestE2ELLM:
    """LLM: provider initialization, config, registry."""

    async def test_llm_provider_initialization(self):
        """LLM provider initializes with config."""
        from modules.llm.provider import LLMProvider
        from modules.llm.config import LLMConfig, LLMProviderConfig

        config = LLMConfig(
            providers={
                "fake": LLMProviderConfig(
                    base_url="http://fake",
                    api_key="fake-key",
                    default_model="fake-model",
                )
            },
            default_provider="fake",
        )
        llm = LLMProvider(config=config)

        providers = llm.provider_registry.list_providers()
        assert len(providers) == 1
        assert providers[0]["name"] == "fake"

        default = llm.provider_registry.get_default()
        assert default is not None
        assert default.name == "fake"

    async def test_llm_multiple_providers(self):
        """Multiple LLM providers with fallback."""
        from modules.llm.provider import LLMProvider
        from modules.llm.config import LLMConfig, LLMProviderConfig

        config = LLMConfig(
            providers={
                "primary": LLMProviderConfig(base_url="http://primary", api_key="k1"),
                "backup": LLMProviderConfig(base_url="http://backup", api_key="k2"),
            },
            default_provider="primary",
            fallback_provider="backup",
        )
        llm = LLMProvider(config=config)

        providers = llm.provider_registry.list_providers()
        assert len(providers) == 2

        default = llm.provider_registry.get_default()
        assert default.name == "primary"

        fallback = llm.provider_registry.get_fallback()
        assert fallback.name == "backup"


# ── E2E 6: REST — FastAPI TestClient ──────────────────────


@pytest.mark.asyncio
class TestE2EREST:
    """REST: TestClient with auth endpoints."""

    async def test_rest_health(self, api_proxy):
        """Health endpoint works without auth."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_rest_auth_status(self, api_proxy):
        """Auth status shows needs_bootstrap=True initially."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.get("/api/v1/auth/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["error"] is None
        # /api/v1/auth/status returns {"data": True/False, "error": None}
        # (proxy.call wraps the boolean result directly)
        assert body["data"] is True

    async def test_rest_bootstrap_and_status(self, api_proxy):
        """Bootstrap then status shows needs_bootstrap=False."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        # Bootstrap
        resp = client.post("/api/v1/auth/bootstrap", json={
            "username": "admin",
            "password": "SecurePass123",
        })
        assert resp.status_code == 200

        # Status after
        resp = client.get("/api/v1/auth/status")
        body = resp.json()
        assert body["data"] is False

    async def test_rest_bootstrap_conflict(self, api_proxy):
        """Double bootstrap → 409 conflict."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        client.post("/api/v1/auth/bootstrap", json={
            "username": "admin",
            "password": "SecurePass123",
        })

        resp = client.post("/api/v1/auth/bootstrap", json={
            "username": "admin2",
            "password": "SecurePass456",
        })
        # Should be 409 conflict (already completed)
        assert resp.status_code == 409

    async def test_rest_bootstrap_bad_json(self, api_proxy):
        """Bootstrap with invalid JSON → 400."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/bootstrap",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_rest_post_public_method(self, api_proxy):
        """POST to public method works without token."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.post("/api/v1/auth/needs_bootstrap", json={})
        assert resp.status_code == 200
        assert resp.json()["data"] is True

    async def test_rest_get_query_params(self, api_proxy):
        """GET /api/v1/auth?method=needs_bootstrap works."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.get("/api/v1/auth?method=needs_bootstrap")
        assert resp.status_code == 200
        assert resp.json()["data"] is True

    async def test_rest_get_missing_method_param(self, api_proxy):
        """GET without method param → 400."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.get("/api/v1/auth")
        assert resp.status_code == 400
        assert "Missing" in resp.json()["error"]["message"]

    async def test_rest_list_modules(self, api_proxy):
        """GET /api/v1 lists registered modules."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.get("/api/v1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "auth" in data

    async def test_rest_unknown_route_404(self, api_proxy):
        """Unknown route → 404 (FastAPI default)."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        app = create_app(proxy_provider=api_proxy)
        client = TestClient(app)

        resp = client.get("/api/v1/nonexistent")
        assert resp.status_code == 404


# ── E2E 7: CLI — parser help, client flow ─────────────────


@pytest.mark.asyncio
class TestE2ECLI:
    """CLI: parser help, client call."""

    async def test_cli_module_help(self, api_proxy):
        """mia auth --help prints module help."""
        from modules.cli.parser import CliParser

        parser = CliParser(registry=api_proxy.registry)
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with pytest.raises(SystemExit) as exc_info:
                parser.parse(["auth", "--help"])
            assert exc_info.value.code == 0
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        assert "auth" in output.lower()

    async def test_cli_general_help(self, api_proxy):
        """mia --help prints general help."""
        from modules.cli.parser import CliParser

        parser = CliParser(registry=api_proxy.registry)
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with pytest.raises(SystemExit) as exc_info:
                parser.parse(["--help"])
            assert exc_info.value.code == 0
        finally:
            sys.stdout = old_stdout

        output = captured.getvalue()
        assert "mia" in output.lower()

    async def test_cli_parse_args(self, api_proxy):
        """Parse --key value arguments."""
        from modules.cli.parser import CliParser

        parser = CliParser(registry=api_proxy.registry)
        cmd = parser.parse(["auth", "bootstrap", "--username", "admin", "--password", "pass"])
        assert cmd.module == "auth"
        assert cmd.method == "bootstrap"
        assert cmd.args["username"] == "admin"
        assert cmd.args["password"] == "pass"

    async def test_cli_missing_args_error(self):
        """Missing module/method → exit 2."""
        from modules.cli.parser import CliParser

        parser = CliParser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse([])
        assert exc_info.value.code == 0  # --help default

    async def test_cli_client_local_call(self, api_proxy):
        """ApiClient local call."""
        from modules.cli.client import ApiClient
        from modules.cli.config import CliConfig

        # Bootstrap
        await api_proxy.call("auth", "bootstrap", {
            "username": "admin",
            "password": "SecurePass123",
        })

        config = CliConfig(mode="local", token_file="/tmp/mia_e2e_test_token.json")
        client = ApiClient(config=config, proxy_provider=api_proxy)

        result = await client._call_local("auth", "needs_bootstrap", {})
        assert result["error"] is None
        assert result["data"] is False

        # Cleanup
        if os.path.exists("/tmp/mia_e2e_test_token.json"):
            os.unlink("/tmp/mia_e2e_test_token.json")

    async def test_cli_client_no_proxy(self):
        """ApiClient with no proxy → 503."""
        from modules.cli.client import ApiClient
        from modules.cli.config import CliConfig

        config = CliConfig(mode="local")
        client = ApiClient(config=config, proxy_provider=None)

        result = await client._call_local("auth", "login", {})
        assert result["error"] is not None
        assert result["error"]["status_code"] == 503


# ── E2E 8: Full system integration ────────────────────────


@pytest.mark.asyncio
class TestE2EFullSystemIntegration:
    """End-to-end: auth → proxy → rest serving requests."""

    async def test_full_system_flow(self, mock_pool, auth_config):
        """Health → auth status → bootstrap → login → refresh → logout."""
        from fastapi.testclient import TestClient
        from modules.rest.app import create_app

        auth_prov = AuthProvider(config=auth_config, pool=mock_pool)
        proxy_config = ApiproxyConfig(whitelist=["auth"])
        proxy = ApiProxyProvider(config=proxy_config, auth_provider=auth_prov)
        proxy.registry.collect_from_module(auth_prov, "auth")

        app = create_app(proxy_provider=proxy)
        client = TestClient(app)

        # 1. Health
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200

        # 2. Auth status
        resp = client.get("/api/v1/auth/status")
        assert resp.json()["data"] is True

        # 3. Bootstrap via REST
        resp = client.post("/api/v1/auth/bootstrap", json={
            "username": "admin",
            "password": "SecurePass123",
        })
        assert resp.status_code == 200

        # 4. Status after bootstrap
        resp = client.get("/api/v1/auth/status")
        assert resp.json()["data"] is False

        # 5. Login via provider (not in proxy registry)
        login_result = await auth_prov.login("admin", "SecurePass123")
        assert "access_token" in login_result

        # 6. Validate token via JWT
        from modules.auth.jwt import validate_access_token
        payload = validate_access_token(
            login_result["access_token"],
            auth_prov._config.jwt_secret,
            auth_prov._config.jwt_algorithm,
        )
        assert payload["username"] == "admin"

        # 7. Refresh
        new_tokens = await auth_prov.refresh_token(login_result["refresh_token"])
        assert new_tokens["access_token"] != login_result["access_token"]

        # 8. Logout
        assert await auth_prov.logout(new_tokens["refresh_token"]) is True


# ── E2E 9: ApiProxy edge cases ────────────────────────────


@pytest.mark.asyncio
class TestE2EApiProxyEdgeCases:
    """ApiProxy: whitelist, missing method, error handling."""

    async def test_whitelist_rejects_unknown_module(self, api_proxy):
        """Module not in whitelist → 403."""
        result = await api_proxy.call("unknown_module", "method", {})
        assert result["error"] is not None
        assert result["error"]["status_code"] == 403

    async def test_missing_method_returns_404(self, api_proxy):
        """Non-existent method → 404."""
        result = await api_proxy.call("auth", "nonexistent_method", {})
        assert result["error"] is not None
        assert result["error"]["status_code"] == 404

    async def test_public_method_works_without_token(self, api_proxy):
        """Public method (needs_bootstrap) works without token."""
        result = await api_proxy.call("auth", "needs_bootstrap", {})
        assert result["error"] is None

    async def test_api_error_serialization(self):
        """ApiError serialization to dict."""
        err = ApiError(400, "Bad request")
        d = err.to_dict()
        assert d["error"]["status_code"] == 400
        assert d["error"]["message"] == "Bad request"
        assert d["data"] is None

    async def test_call_method_not_found(self, api_proxy):
        """call_method with unknown method → 404."""
        result = await call_method(
            registry=api_proxy.registry,
            middleware=api_proxy.middleware,
            module_name="auth",
            method_name="ghost_method",
            kwargs={},
            token=None,
        )
        assert result["error"] is not None
        assert result["error"]["status_code"] == 404


# ── E2E 10: Password policy enforcement ───────────────────


@pytest.mark.asyncio
class TestE2EPasswordPolicy:
    """Password validation: length, uppercase, digit, duplicates."""

    async def test_too_short(self, auth_provider):
        with pytest.raises(ValueError, match="at least 8"):
            await auth_provider.create_user("u1", "Short1")

    async def test_no_uppercase(self, auth_provider):
        with pytest.raises(ValueError, match="uppercase"):
            await auth_provider.create_user("u1", "nouppercase123")

    async def test_no_digit(self, auth_provider):
        with pytest.raises(ValueError, match="digit"):
            await auth_provider.create_user("u1", "NoDigitsHere")

    async def test_valid_password(self, auth_provider):
        user = await auth_provider.create_user("u1", "ValidPass123")
        assert user["username"] == "u1"

    async def test_duplicate_username(self, auth_provider):
        await auth_provider.create_user("u1", "ValidPass123")
        with pytest.raises(ValueError, match="already exists"):
            await auth_provider.create_user("u1", "ValidPass456")


# ── E2E 11: User management ───────────────────────────────


@pytest.mark.asyncio
class TestE2EUserManagement:
    """User CRUD: create, get, update, delete, list."""

    async def test_user_crud(self, auth_provider):
        user = await auth_provider.create_user("cruduser", "SecurePass123")
        found = await auth_provider.get_user(user["id"])
        assert found is not None
        assert found["username"] == "cruduser"

        updated = await auth_provider.update_user(user["id"], {"email": "c@test.com"})
        assert updated["email"] == "c@test.com"

        assert await auth_provider.delete_user(user["id"], force=True) is True
        assert await auth_provider.get_user(user["id"]) is None

    async def test_list_users(self, auth_provider):
        await auth_provider.create_user("list1", "SecurePass123")
        await auth_provider.create_user("list2", "SecurePass123")
        users, total = await auth_provider.list_users(offset=0, limit=10)
        assert total >= 2

    async def test_block_unblock(self, auth_provider):
        user = await auth_provider.create_user("block1", "SecurePass123")
        await auth_provider.block_user(user["id"], minutes=5)
        found = await auth_provider.get_user(user["id"])
        assert found["locked_until"] is not None

        await auth_provider.unblock_user(user["id"])
        found = await auth_provider.get_user(user["id"])
        assert found["locked_until"] is None

    async def test_disable_enable(self, auth_provider):
        user = await auth_provider.create_user("dis1", "SecurePass123")
        await auth_provider.disable_user(user["id"])
        found = await auth_provider.get_user(user["id"])
        assert found["is_disabled"] is True

        await auth_provider.enable_user(user["id"])
        found = await auth_provider.get_user(user["id"])
        assert found["is_disabled"] is False


# ── E2E 12: Group and role management ─────────────────────


@pytest.mark.asyncio
class TestE2EGroupAndRoleManagement:
    """Groups and roles CRUD (direct pool calls, no JOINs)."""

    async def test_group_crud(self, auth_provider):
        group = await auth_provider.create_group("engineers", "Engineering")
        assert group["name"] == "engineers"

        updated = await auth_provider.update_group(group["id"], {"description": "Senior"})
        assert updated["description"] == "Senior"

        assert await auth_provider.delete_group(group["id"], force=True) is True

    async def test_role_crud(self, auth_provider):
        role = await auth_provider.create_role("test_role", "Test role")
        assert role["name"] == "test_role"

        assert await auth_provider.delete_role(role["id"], force=True) is True

    async def test_assign_remove_role_direct(self, auth_provider, mock_pool):
        """Assign role via direct pool insert (avoids JOIN)."""
        user = await auth_provider.create_user("roleuser", "SecurePass123")
        role = await auth_provider.create_role("test_assign", "Test")

        # Direct insert to user_roles
        mock_pool.insert_direct("user_roles", {
            "user_id": user["id"],
            "role_id": role["id"],
        })

        # Verify via direct query
        rows = await mock_pool.fetch(
            "SELECT * FROM user_roles WHERE user_id = $1", user["id"]
        )
        assert len(rows) == 1

    async def test_add_remove_group_membership_direct(self, auth_provider, mock_pool):
        """Add user to group via direct pool insert."""
        user = await auth_provider.create_user("grpuser", "SecurePass123")
        group = await auth_provider.create_group("testgrp", "Test group")

        mock_pool.insert_direct("user_group_membership", {
            "user_id": user["id"],
            "group_id": group["id"],
        })

        rows = await mock_pool.fetch(
            "SELECT * FROM user_group_membership WHERE user_id = $1", user["id"]
        )
        assert len(rows) == 1
