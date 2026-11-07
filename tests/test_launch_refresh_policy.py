#!/usr/bin/env python3
"""Unit tests for the 6-day launch refresh expiration policy and patch script."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.patch_launch_refresh_policy import (
    MARKER,
    POLICY_NEW,
    POLICY_OLD,
    RECOVER_NEW,
    RECOVER_OLD,
    patch_repository,
)


def is_launch_refresh_due(now: datetime, last_success: datetime | None, profile_expiration: datetime | None) -> bool:
    """Python reference equivalent of LiveContainerRefreshPolicy.isLaunchRefreshDue."""
    six_days = timedelta(days=6)
    if last_success is not None:
        return (now - last_success) >= six_days
    if profile_expiration is not None:
        return (profile_expiration - now) <= timedelta(days=1)
    return False


class LaunchRefreshPolicyTests(unittest.TestCase):
    def test_policy_within_six_days_does_not_trigger(self):
        now = datetime(2026, 11, 2, 17, 0, 0)
        # Refreshed 1 hour ago (like 4:00 AM vs 5:00 AM)
        self.assertFalse(is_launch_refresh_due(now, now - timedelta(hours=1), None))
        # Refreshed 12 hours ago (like 5:00 AM vs 17:00 PM)
        self.assertFalse(is_launch_refresh_due(now, now - timedelta(hours=12), None))
        # Refreshed 3 days ago
        self.assertFalse(is_launch_refresh_due(now, now - timedelta(days=3), None))
        # Refreshed 5 days and 23 hours ago
        self.assertFalse(is_launch_refresh_due(now, now - timedelta(days=5, hours=23), None))

    def test_policy_at_or_beyond_six_days_triggers_fallback(self):
        now = datetime(2026, 11, 2, 17, 0, 0)
        # Exactly 6 days ago
        self.assertTrue(is_launch_refresh_due(now, now - timedelta(days=6), None))
        # 6.5 days ago
        self.assertTrue(is_launch_refresh_due(now, now - timedelta(days=6, hours=12), None))
        # 7 days ago
        self.assertTrue(is_launch_refresh_due(now, now - timedelta(days=7), None))

    def test_policy_profile_expiration_fallback_when_last_success_nil(self):
        now = datetime(2026, 11, 2, 17, 0, 0)
        # No previous success recorded, but profile expires in 12 hours (< 24h)
        self.assertTrue(is_launch_refresh_due(now, None, now + timedelta(hours=12)))
        # Profile expires in 2 days (> 24h)
        self.assertFalse(is_launch_refresh_due(now, None, now + timedelta(days=2)))
        # Neither available
        self.assertFalse(is_launch_refresh_due(now, None, None))

    def test_patch_repository_applies_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            app_dir = tmp_path / "LiveContainerSwiftUI/App"
            app_dir.mkdir(parents=True)
            app_delegate = app_dir / "AppDelegate.swift"

            content = f"// Sample file\n{POLICY_OLD}\n// other code\n{RECOVER_OLD}\n"
            app_delegate.write_text(content, encoding="utf-8")

            # First apply should succeed
            applied = patch_repository(tmp_path)
            self.assertTrue(applied)

            patched_content = app_delegate.read_text(encoding="utf-8")
            self.assertIn(MARKER, patched_content)
            self.assertIn(POLICY_NEW, patched_content)
            self.assertIn(RECOVER_NEW, patched_content)

            # Second apply should be idempotent and return False
            applied_again = patch_repository(tmp_path)
            self.assertFalse(applied_again)


if __name__ == "__main__":
    unittest.main()
