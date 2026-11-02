#!/usr/bin/env python3
"""Patch LiveContainer with 6-day expiration guard for launch_or_resume.

Replaces the eager 6-hour launch_or_resume check with a 6-day expiration policy,
preventing spurious network preflight failures on normal app launches while
preserving emergency expiration fallback when nearing 7-day certificate expiry.
"""
from __future__ import annotations

from pathlib import Path
import sys

MARKER = "LC_LAUNCH_REFRESH_POLICY_SIX_DAYS_V1"

POLICY_OLD = """    static func workIsDue(now: Date, eligible: Date?, retry: Date?, pendingHandoff: Bool,
                          retryExhausted: Bool, manual: Bool) -> Bool {
        if pendingHandoff { return false }
        if manual { return true }
        if retryExhausted { return false }
        if let retry, retry > now { return false }
        if let eligible, eligible > now { return false }
        return true
    }"""

POLICY_NEW = """    static func workIsDue(now: Date, eligible: Date?, retry: Date?, pendingHandoff: Bool,
                          retryExhausted: Bool, manual: Bool) -> Bool {
        if pendingHandoff { return false }
        if manual { return true }
        if retryExhausted { return false }
        if let retry, retry > now { return false }
        if let eligible, eligible > now { return false }
        return true
    }

    // LC_LAUNCH_REFRESH_POLICY_SIX_DAYS_V1
    static func isLaunchRefreshDue(now: Date, lastSuccess: Date?, profileExpiration: Date?) -> Bool {
        let sixDays: TimeInterval = 6 * 24 * 60 * 60
        if let lastSuccess {
            return now.timeIntervalSince(lastSuccess) >= sixDays
        }
        if let profileExpiration {
            return profileExpiration.timeIntervalSince(now) <= 24 * 60 * 60
        }
        return false
    }"""

RECOVER_OLD = """        observeMissedDeadline(now: Date())
        guard defaults.bool(forKey: enabledKey), defaults.object(forKey: earliestEligibleKey) != nil,
              compactWorkIsDue(now: Date()) else { return }
        Task { @MainActor in await execute(source: "launch_or_resume") }"""

RECOVER_NEW = """        observeMissedDeadline(now: Date())
        guard defaults.bool(forKey: enabledKey) else { return }
        let now = Date()
        let lastSuccess = defaults.object(forKey: lastSuccessfulKey) as? Date
        let profileExpiration = (try? LiveContainerHostProfile.read(
            at: Bundle.main.bundleURL.appendingPathComponent("embedded.mobileprovision"),
            expectedBundleID: Bundle.main.bundleIdentifier ?? ""))?.expiration
        guard LiveContainerRefreshPolicy.isLaunchRefreshDue(
            now: now,
            lastSuccess: lastSuccess,
            profileExpiration: profileExpiration
        ), compactWorkIsDue(now: now) else { return }
        Task { @MainActor in await execute(source: "launch_or_resume") }"""


def patch_repository(target_dir: Path) -> bool:
    app_delegate = target_dir / "LiveContainerSwiftUI/App/AppDelegate.swift"
    if not app_delegate.is_file():
        raise FileNotFoundError(f"Missing AppDelegate at {app_delegate}")

    content = app_delegate.read_text(encoding="utf-8")
    if MARKER in content:
        return False

    if POLICY_OLD not in content:
        raise ValueError("Could not find LiveContainerRefreshPolicy.workIsDue anchor in AppDelegate.swift")
    if RECOVER_OLD not in content:
        raise ValueError("Could not find recoverAfterLaunchOrResume anchor in AppDelegate.swift")

    content = content.replace(POLICY_OLD, POLICY_NEW, 1)
    content = content.replace(RECOVER_OLD, RECOVER_NEW, 1)
    app_delegate.write_text(content, encoding="utf-8")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: patch_launch_refresh_policy.py <path-to-LiveContainer>", file=sys.stderr)
        sys.exit(1)
    applied = patch_repository(Path(sys.argv[1]))
    print(f"Patch applied: {applied}")
