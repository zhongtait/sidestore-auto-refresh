#!/usr/bin/env python3
"""Unit tests for the refresh recovery deadlock & auto-reconcile patch script."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.patch_refresh_recovery_deadlock import (
    ACCEPT_NEW,
    ACCEPT_OLD,
    AUTO_RECONCILE_OBSERVE_NEW_1,
    AUTO_RECONCILE_OBSERVE_NEW_2,
    AUTO_RECONCILE_OBSERVE_OLD_1,
    AUTO_RECONCILE_OBSERVE_OLD_2,
    BANNER_NEW,
    BANNER_OLD,
    BANNER_VIEW_NEW,
    BANNER_VIEW_OLD,
    LEASE_NEW,
    LEASE_OLD,
    MARKER,
    PRESENT_ERROR_NEW,
    PRESENT_ERROR_OLD,
    RECONCILE_NEW,
    RECONCILE_OLD,
    SIDESTORE_SNAPSHOT_NEW,
    SIDESTORE_SNAPSHOT_OLD,
    patch_repository,
)


class RefreshRecoveryDeadlockPatchTests(unittest.TestCase):
    def _create_fixture_tree(self, root: Path) -> tuple[Path, Path]:
        ss_dir = root / "SideStoreSupport"
        shell_dir = root / "LiveContainerSwiftUI/Views"
        ss_dir.mkdir(parents=True, exist_ok=True)
        shell_dir.mkdir(parents=True, exist_ok=True)

        side_store = ss_dir / "SideStore.swift"
        v3_shell = shell_dir / "V3UnifiedShell.swift"

        side_store.write_text(
            f"// SideStore fixture\n{LEASE_OLD}\n{SIDESTORE_SNAPSHOT_OLD}\n",
            encoding="utf-8",
        )
        v3_shell.write_text(
            "\n".join(
                [
                    "// V3UnifiedShell fixture",
                    LEASE_OLD,
                    BANNER_OLD,
                    BANNER_VIEW_OLD,
                    RECONCILE_OLD,
                    PRESENT_ERROR_OLD,
                    ACCEPT_OLD,
                    AUTO_RECONCILE_OBSERVE_OLD_1,
                    AUTO_RECONCILE_OBSERVE_OLD_2,
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return side_store, v3_shell

    def test_patch_applies_cleanly_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            side_store, v3_shell = self._create_fixture_tree(root)

            applied = patch_repository(root)
            self.assertTrue(applied)

            ss_content = side_store.read_text(encoding="utf-8")
            shell_content = v3_shell.read_text(encoding="utf-8")

            self.assertIn(MARKER, ss_content)
            self.assertIn(LEASE_NEW, ss_content)
            self.assertIn(SIDESTORE_SNAPSHOT_NEW, ss_content)
            self.assertIn("isRefreshOwnerLost", ss_content)
            self.assertIn("effectiveRecoveryHold", ss_content)
            self.assertIn("retireOrphanedRefreshServiceIfNeeded", ss_content)

            self.assertIn(MARKER, shell_content)
            self.assertIn(LEASE_NEW, shell_content)
            self.assertIn(ACCEPT_NEW, shell_content)
            self.assertIn(RECONCILE_NEW, shell_content)
            self.assertIn(AUTO_RECONCILE_OBSERVE_NEW_1, shell_content)
            self.assertIn(AUTO_RECONCILE_OBSERVE_NEW_2, shell_content)
            self.assertIn(BANNER_NEW, shell_content)
            self.assertIn(BANNER_VIEW_NEW, shell_content)
            self.assertIn(PRESENT_ERROR_NEW, shell_content)
            self.assertIn("autoReconcileLostRefresh(runID: runID)", shell_content)
            self.assertIn("autoRestartOrphanedRefreshService()", shell_content)
            self.assertIn("updateLocalHostRecoveryHold()", shell_content)
            self.assertIn("clearReconciledRefreshState(runID: runID)", shell_content)
            self.assertIn("liveContainerAutoRefreshUncertainMutationRunID", shell_content)
            self.assertNotIn("reload(manual: false)", RECONCILE_NEW)

            # Second invocation is idempotent and makes no further changes
            applied_again = patch_repository(root)
            self.assertFalse(applied_again)
            self.assertEqual(ss_content, side_store.read_text(encoding="utf-8"))
            self.assertEqual(shell_content, v3_shell.read_text(encoding="utf-8"))

    def test_fails_closed_on_missing_anchor(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            _, v3_shell = self._create_fixture_tree(root)
            # Corrupt RECONCILE_OLD anchor in V3UnifiedShell.swift
            v3_shell.write_text(
                v3_shell.read_text(encoding="utf-8").replace(
                    "func reconcileLostRefreshAfterDeviceCheck()",
                    "func driftedReconcileMethod()",
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                patch_repository(root)

    def test_upgrades_v6_0_0_direct_host_bridge_when_present(self):
        from scripts import patch_refresh_result_bridge as bridge

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            self._create_fixture_tree(root)
            ss_dir = root / "SideStoreSupport"
            (ss_dir / "XPCServer.h").write_text(
                "- (void)onConnection:(NSXPCConnection*)connection;\n"
                "- (void)refreshAllAppsWithIdentifier:(NSString*)identifier mangledTypeName:(NSString *)mangledTypeName refreshRunID:(NSString* _Nullable)refreshRunID;\n",
                encoding="utf-8",
            )
            (ss_dir / "XPCClient.m").write_text(
                "@protocol V3CommandService\n+ (void)execute:(NSData *)request reply:(void (^)(NSData *))reply;\n@end\n"
                "                                     server:(id <RefreshServer> _Nonnull)server;\n"
                + bridge.LEGACY_HOST_METHOD,
                encoding="utf-8",
            )
            (ss_dir / "SideStoreClient.swift").write_text(
                "    @objc(performRefreshForRealWithIdentifier:mangledTypeName:server:)\n",
                encoding="utf-8",
            )
            self.assertTrue(patch_repository(root))
            self.assertIn("V3_REFRESH_CALLBACK_ADMISSION_V1", (ss_dir / "XPCServer.h").read_text(encoding="utf-8"))
            self.assertIn("V3_DIRECT_HOST_REFRESH_V1", (ss_dir / "XPCClient.m").read_text(encoding="utf-8"))
            self.assertIn(bridge.DIRECT_HOST_FAILURE, (ss_dir / "SideStoreClient.swift").read_text(encoding="utf-8"))
            self.assertFalse(patch_repository(root))


if __name__ == "__main__":
    unittest.main()
