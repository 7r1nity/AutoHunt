from __future__ import annotations

from .models import User, UserRole


TEST_MANAGER_EMAIL = "autohunt-employer@example.com"
TEST_MANAGER_NAME = "AutoHunt 统一测试招聘方"
TEST_MANAGER_COMPANY = "AutoHunt 测试招聘中心"


def is_test_manager(user: User) -> bool:
    return user.role == UserRole.employer and user.email.strip().lower() == TEST_MANAGER_EMAIL