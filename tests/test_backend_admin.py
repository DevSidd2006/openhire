"""
Admin console backend: user_type == 'admin', require_admin, the audit log,
moderation flags, and the /admin router (user management, moderation,
metrics, impersonation).

No self-service admin signup exists anywhere - see
schemas/auth.py:SignupRequest's user_type pattern, unchanged by this module.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.security import PrincipalType
from services.auth_service import AuthService
from repositories.memory import InMemoryUserRepository
from core.config import AppSettings


class TestAdminPrincipalType:
    def test_verify_access_token_maps_admin_user_type_to_admin_principal(self):
        settings = AppSettings()
        service = AuthService(user_repository=InMemoryUserRepository(), settings=settings)
        assert service._map_user_type_to_principal_type("admin") is PrincipalType.ADMIN

    def test_admin_gets_admin_scopes(self):
        settings = AppSettings()
        service = AuthService(user_repository=InMemoryUserRepository(), settings=settings)
        scopes = service._get_scopes_for_user_type("admin")
        assert "admin:read" in scopes
        assert "admin:write" in scopes
