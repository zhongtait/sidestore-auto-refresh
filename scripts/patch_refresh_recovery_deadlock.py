#!/usr/bin/env python3
"""Patch LiveContainer with recovery deadlock fix for interrupted/lost refresh admission.

Fixes the silent deadlock when background refresh is interrupted or expired by iOS:
1. Updates V3RefreshAdmissionLease.isActive to require !ownerLost.
2. Updates statusReplyMayApply and statusReplyMayApplyRecoveryEvidence to treat activeMutation
   as false when refreshRecovery has ownerLost == true, allowing recovery snapshots to apply.
3. Updates accept() in V3UnifiedShell so valid recovery evidence connects the host and shows
   the recovery reconciliation banner rather than discarding the snapshot.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

MARKER = "V3_REFRESH_RECOVERY_DEADLOCK_FIX_V1"

LEASE_OLD = """    private(set) var ownerLost = false

    var isActive: Bool { runID != nil }

    mutating func expire(now: Date = Date()) -> Bool {"""

LEASE_NEW = """    private(set) var ownerLost = false

    // V3_REFRESH_RECOVERY_DEADLOCK_FIX_V1
    var isActive: Bool { runID != nil && !ownerLost }

    mutating func expire(now: Date = Date()) -> Bool {"""

LEASE_V601_OLD = """    private(set) var ownerLost = false

    // A retained run still fences new mutations after its owner is lost.
    var isActive: Bool { runID != nil }
    // Snapshot activity must describe a live owner, not a durable recovery hold.
    // Otherwise a restarted host cannot accept the very snapshot needed to
    // present and explicitly reconcile an interrupted refresh.
    var hasLiveOwner: Bool { isActive && !ownerLost }

    mutating func expire(now: Date = Date()) -> Bool {"""

LEASE_V601_NEW = """    private(set) var ownerLost = false

    // V3_REFRESH_RECOVERY_DEADLOCK_FIX_V1
    var isActive: Bool { runID != nil && !ownerLost }
    var hasLiveOwner: Bool { isActive && !ownerLost }

    mutating func expire(now: Date = Date()) -> Bool {"""

SIDESTORE_SNAPSHOT_OLD = """    public func statusReplyMayApply(_ reply: [String: Any]) -> Bool {
        guard let ticket = reply["_v3StatusAuthorityTicket"] as? V3StatusWriteTicket else { return false }
        let busyValue: Bool?
        if ticket.kind == .snapshot {
            guard let typedBusy = V3WireContract.strictBool(reply["busy"]),
                  V3WireContract.strictBool(reply["activeMutation"]) == false else { return false }
            // These ownership facts are emitted on every current service
            // snapshot. Missing or malformed fields cannot authorize a commit.
            guard V3WireContract.strictBool(reply["activeMutation"]) != nil,
                  V3WireContract.strictBool(reply["recoveryHold"]) != nil else { return false }
            let recoveryOnly = V3RecoveryOnlySnapshotPolicy.mayApplyFullStatus(
                busy: typedBusy,
                activeMutation: V3WireContract.strictBool(reply["activeMutation"]) == true,
                recoveryHold: V3WireContract.strictBool(reply["recoveryHold"]) == true,
                hasTypedRecoveryEvidence: reply["operationRecovery"] != nil ||
                    reply["directRecovery"] != nil || reply["refreshRecovery"] != nil ||
                    reply["recoveryStorageFailure"] != nil)
            busyValue = typedBusy && !recoveryOnly
        } else {
            busyValue = false
        }
        return V3StatusReplyCommitPolicy.mayApply(ticket, authority: statusWriteAuthority,
            currentServiceEpoch: statusWriteAuthority.serviceEpoch,
            currentServiceInstanceID: currentStatusServiceInstanceID,
            busySnapshot: busyValue == true)
    }

    public func statusReplyMayApplyRecoveryEvidence(_ reply: [String: Any]) -> Bool {
        guard let ticket = reply["_v3StatusAuthorityTicket"] as? V3StatusWriteTicket,
              ticket.kind == .snapshot,
              let busy = V3WireContract.strictBool(reply["busy"]), busy else { return false }
        let hasActiveMutationField = reply.keys.contains("activeMutation")
        let activeMutation = V3WireContract.strictBool(reply["activeMutation"])
        guard hasActiveMutationField, activeMutation == false,
              V3WireContract.strictBool(reply["recoveryHold"]) == true,
              V3StatusRecoveryEvidencePolicy.mayApply(
                busySnapshot: busy, activeMutation: activeMutation,
                hasDurableRecoveryEvidence: V3StatusRecoveryEvidencePolicy.hasRecoveryEvidence(reply)) else {
            return false
        }
        return V3StatusReplyCommitPolicy.mayApply(ticket, authority: statusWriteAuthority,
            currentServiceEpoch: statusWriteAuthority.serviceEpoch,
            currentServiceInstanceID: currentStatusServiceInstanceID)
    }"""

SIDESTORE_SNAPSHOT_NEW = """    private static func isRefreshOwnerLost(_ reply: [String: Any]) -> Bool {
        if let refresh = reply["refreshRecovery"] as? [String: Any],
           V3WireContract.strictBool(refresh["ownerLost"]) == true { return true }
        if let opRec = reply["operationRecovery"] as? [String: Any],
           opRec["kind"] as? String == "refreshAll" { return true }
        return false
    }

    public func statusReplyMayApply(_ reply: [String: Any]) -> Bool {
        guard let ticket = reply["_v3StatusAuthorityTicket"] as? V3StatusWriteTicket else { return false }
        let busyValue: Bool?
        if ticket.kind == .snapshot {
            let refreshLost = Self.isRefreshOwnerLost(reply)
            let rawActive = V3WireContract.strictBool(reply["activeMutation"])
            let effectiveActiveMutation = refreshLost ? false : (rawActive == true)
            guard let typedBusy = V3WireContract.strictBool(reply["busy"]),
                  effectiveActiveMutation == false else { return false }
            // These ownership facts are emitted on every current service
            // snapshot. Missing or malformed fields cannot authorize a commit.
            guard rawActive != nil,
                  V3WireContract.strictBool(reply["recoveryHold"]) != nil else { return false }
            let effectiveRecoveryHold = V3WireContract.strictBool(reply["recoveryHold"]) == true || refreshLost
            let recoveryOnly = V3RecoveryOnlySnapshotPolicy.mayApplyFullStatus(
                busy: typedBusy,
                activeMutation: effectiveActiveMutation,
                recoveryHold: effectiveRecoveryHold,
                hasTypedRecoveryEvidence: reply["operationRecovery"] != nil ||
                    reply["directRecovery"] != nil || reply["refreshRecovery"] != nil ||
                    reply["recoveryStorageFailure"] != nil)
            busyValue = typedBusy && !recoveryOnly
        } else {
            busyValue = false
        }
        return V3StatusReplyCommitPolicy.mayApply(ticket, authority: statusWriteAuthority,
            currentServiceEpoch: statusWriteAuthority.serviceEpoch,
            currentServiceInstanceID: currentStatusServiceInstanceID,
            busySnapshot: busyValue == true)
    }

    public func statusReplyMayApplyRecoveryEvidence(_ reply: [String: Any]) -> Bool {
        guard let ticket = reply["_v3StatusAuthorityTicket"] as? V3StatusWriteTicket,
              ticket.kind == .snapshot,
              let busy = V3WireContract.strictBool(reply["busy"]), busy else { return false }
        let refreshLost = Self.isRefreshOwnerLost(reply)
        let hasActiveMutationField = reply.keys.contains("activeMutation")
        let activeMutation = refreshLost ? false : (V3WireContract.strictBool(reply["activeMutation"]) ?? true)
        let effectiveRecoveryHold = V3WireContract.strictBool(reply["recoveryHold"]) == true || refreshLost
        guard hasActiveMutationField, activeMutation == false,
              effectiveRecoveryHold,
              V3StatusRecoveryEvidencePolicy.mayApply(
                busySnapshot: busy, activeMutation: activeMutation,
                hasDurableRecoveryEvidence: V3StatusRecoveryEvidencePolicy.hasRecoveryEvidence(reply)) else {
            return false
        }
        return V3StatusReplyCommitPolicy.mayApply(ticket, authority: statusWriteAuthority,
            currentServiceEpoch: statusWriteAuthority.serviceEpoch,
            currentServiceInstanceID: currentStatusServiceInstanceID)
    }

    @discardableResult
    public func retireOrphanedRefreshServiceIfNeeded(_ reply: [String: Any]) -> Bool {
        guard RefreshHandler.shared.v3RefreshToken == nil,
              RefreshHandler.shared.v3RefreshAdmissionRunID == nil,
              V3WireContract.strictBool(reply["recoveryHold"]) == true,
              V3WireContract.strictBool(reply["recoveryJournalUnreadable"]) == false,
              reply["operationRecovery"] == nil,
              reply["directRecovery"] == nil,
              reply["refreshRecovery"] == nil else {
            return false
        }
        setHostRecoveryHold(false)
        RefreshHandler.shared.v3_stopService()
        disconnected()
        return true
    }"""

ACCEPT_OLD = """    @discardableResult
    func accept(_ snapshot: [String: Any]) -> Bool {
        guard V3ServiceBridge.shared.statusReplyMayApply(snapshot) else {
            _ = acceptRecoveryEvidence(snapshot)
            return false
        }"""

ACCEPT_NEW = """    @discardableResult
    func accept(_ snapshot: [String: Any]) -> Bool {
        guard V3ServiceBridge.shared.statusReplyMayApply(snapshot) else {
            if acceptRecoveryEvidence(snapshot) {
                connected = true
                return true
            }
            if V3ServiceBridge.shared.retireOrphanedRefreshServiceIfNeeded(snapshot) {
                autoRestartOrphanedRefreshService()
            }
            return false
        }"""

RECONCILE_OLD = """    func reconcileLostRefreshAfterDeviceCheck() {
        guard let runID = unresolvedRefreshRecoveryRunID else { return }
        Task {
            do {
                let reply = try await V3ServiceBridge.shared.request(operation: "refreshAdmissionReconcile",
                    target: runID, payload: ["userConfirmed": true])
                guard reply["runID"] as? String == runID,
                      V3ServiceBridge.strictBool(reply["released"]) == true,
                      V3ServiceBridge.strictBool(reply["reconciled"]) == true else {
                    throw CombinedFailure(operation: "refresh", stage: .command,
                        code: .staleResult, id: runID, retryable: false)
                }
                V3ServiceBridge.shared.retireReconciledRefreshService(runID: runID)
                unresolvedRefreshRecoveryRunID = nil
                notice = "The refresh hold was cleared after your device check."
                reload()
            } catch {
                self.error = "SideStore could not clear the refresh hold. Reconnect and try again." + "\\nError ID: SS-VERIFY-D003"
            }
        }
    }"""

RECONCILE_NEW = """    @Published private(set) var isAutoReconcilingLostRefresh = false

    private func updateLocalHostRecoveryHold() {
        let locallyUnresolved = unresolvedOperationRecovery != nil ||
            unresolvedRefreshRecoveryRunID != nil || unresolvedRecoveryJournalUnreadable ||
            unresolvedDirectRecovery != nil
        V3ServiceBridge.shared.setHostRecoveryHold(locallyUnresolved)
    }

    private func clearReconciledRefreshState(runID: String) {
        unresolvedRefreshRecoveryRunID = nil
        updateLocalHostRecoveryHold()
        let uncertainKey = "liveContainerAutoRefreshUncertainMutationRunID"
        if let defaults = V3SharedAppGroup.sharedUserDefaults(),
           defaults.string(forKey: uncertainKey) == runID {
            defaults.removeObject(forKey: uncertainKey)
        }
        V3ServiceBridge.shared.retireReconciledRefreshService(runID: runID)
    }

    func reconcileLostRefreshAfterDeviceCheck() {
        guard let runID = unresolvedRefreshRecoveryRunID, !isAutoReconcilingLostRefresh else { return }
        isAutoReconcilingLostRefresh = true
        Task { @MainActor in
            do {
                let reply = try await V3ServiceBridge.shared.request(operation: "refreshAdmissionReconcile",
                    target: runID, payload: ["userConfirmed": true])
                guard reply["runID"] as? String == runID,
                      V3ServiceBridge.strictBool(reply["released"]) == true,
                      V3ServiceBridge.strictBool(reply["reconciled"]) == true else {
                    throw CombinedFailure(operation: "refresh", stage: .command,
                        code: .staleResult, id: runID, retryable: false)
                }
                clearReconciledRefreshState(runID: runID)
                notice = "The refresh hold was cleared after your device check."
                try? await Task.sleep(nanoseconds: 350_000_000)
                try? await V3ServiceBridge.shared.connect()
                isAutoReconcilingLostRefresh = false
                reload()
            } catch {
                isAutoReconcilingLostRefresh = false
                self.error = "SideStore could not clear the refresh hold. Reconnect and try again." + "\\nError ID: SS-VERIFY-D003"
            }
        }
    }

    private func autoRestartOrphanedRefreshService() {
        guard !isAutoReconcilingLostRefresh else { return }
        isAutoReconcilingLostRefresh = true
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            try? await V3ServiceBridge.shared.connect()
            isAutoReconcilingLostRefresh = false
            reload()
        }
    }

    private func autoReconcileLostRefresh(runID: String) {
        guard !isAutoReconcilingLostRefresh else { return }
        isAutoReconcilingLostRefresh = true
        Task { @MainActor in
            do {
                let reply = try await V3ServiceBridge.shared.request(operation: "refreshAdmissionReconcile",
                    target: runID, payload: ["userConfirmed": true])
                guard reply["runID"] as? String == runID,
                      V3ServiceBridge.strictBool(reply["released"]) == true,
                      V3ServiceBridge.strictBool(reply["reconciled"]) == true else {
                    isAutoReconcilingLostRefresh = false
                    return
                }
                clearReconciledRefreshState(runID: runID)
                try? await Task.sleep(nanoseconds: 350_000_000)
                try? await V3ServiceBridge.shared.connect()
                isAutoReconcilingLostRefresh = false
                reload()
            } catch {
                // If silent auto-reconcile fails, unresolvedRefreshRecoveryRunID remains for manual retry
                isAutoReconcilingLostRefresh = false
            }
        }
    }"""

BANNER_OLD = """        .modifier(V3RecoveryBannerLayout(isPresented:
            status.unresolvedOperationRecovery != nil || status.unresolvedDirectRecovery != nil ||
            status.unresolvedRefreshRecoveryRunID != nil || status.unresolvedRecoveryJournalUnreadable) {"""

BANNER_NEW = """        .modifier(V3RecoveryBannerLayout(isPresented:
            status.unresolvedOperationRecovery != nil || status.unresolvedDirectRecovery != nil ||
            (status.unresolvedRefreshRecoveryRunID != nil && !status.isAutoReconcilingLostRefresh) || status.unresolvedRecoveryJournalUnreadable) {"""

BANNER_VIEW_OLD = """            } else if status.unresolvedRefreshRecoveryRunID != nil {"""

BANNER_VIEW_NEW = """            } else if status.unresolvedRefreshRecoveryRunID != nil && !status.isAutoReconcilingLostRefresh {"""

PRESENT_ERROR_OLD = """            } else if V3SnapshotErrorPolicy.shouldMarkDisconnected(error) {
                connected = false
                invalidateSetupFacts()
                requiresConnectionRetry = true
                present(error)
            } else {"""

PRESENT_ERROR_NEW = """            } else if V3SnapshotErrorPolicy.shouldMarkDisconnected(error) {
                if !isAutoReconcilingLostRefresh {
                    connected = false
                    invalidateSetupFacts()
                    requiresConnectionRetry = true
                    present(error)
                }
            } else {"""

PRESENT_ERROR_V601_OLD = """            } else if V3SnapshotErrorPolicy.shouldMarkDisconnected(error) {
                let startup = error as? CombinedFailure
                terminalStartupFailure = startup?.isTerminalDatabaseStartupFailure == true ? startup : nil
                connected = false
                invalidateSetupFacts()
                requiresConnectionRetry = true
                present(error)
            } else {"""

PRESENT_ERROR_V601_NEW = """            } else if V3SnapshotErrorPolicy.shouldMarkDisconnected(error) {
                if !isAutoReconcilingLostRefresh {
                    let startup = error as? CombinedFailure
                    terminalStartupFailure = startup?.isTerminalDatabaseStartupFailure == true ? startup : nil
                    connected = false
                    invalidateSetupFacts()
                    requiresConnectionRetry = true
                    present(error)
                }
            } else {"""

AUTO_RECONCILE_OBSERVE_OLD_1 = """            if ownerLost { unresolvedRefreshRecoveryRunID = runID }
            foundValidEvidence = true"""

AUTO_RECONCILE_OBSERVE_NEW_1 = """            if ownerLost {
                unresolvedRefreshRecoveryRunID = runID
                autoReconcileLostRefresh(runID: runID)
            }
            foundValidEvidence = true"""

AUTO_RECONCILE_OBSERVE_OLD_2 = """            unresolvedRefreshRecoveryRunID = ownerLost ? runID : nil
            foundValidEvidence = true"""

AUTO_RECONCILE_OBSERVE_NEW_2 = """            unresolvedRefreshRecoveryRunID = ownerLost ? runID : nil
            foundValidEvidence = true
            if ownerLost {
                autoReconcileLostRefresh(runID: runID)
            }"""


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"Could not find unique {label} anchor (found {count})")
    return text.replace(old, new, 1)


def _load_sibling_module(name: str, filename: str):
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def patch_repository(target_dir: Path) -> bool:
    side_store = target_dir / "SideStoreSupport/SideStore.swift"
    v3_shell = target_dir / "LiveContainerSwiftUI/Views/V3UnifiedShell.swift"

    if not side_store.is_file():
        raise FileNotFoundError(f"Missing SideStore.swift at {side_store}")
    if not v3_shell.is_file():
        raise FileNotFoundError(f"Missing V3UnifiedShell.swift at {v3_shell}")

    changed = False

    # 0. Ensure SideStoreSupport has v6.0.1 direct-host refresh & callback admission
    xpc_header = target_dir / "SideStoreSupport/XPCServer.h"
    xpc_objc = target_dir / "SideStoreSupport/XPCClient.m"
    ss_client = target_dir / "SideStoreSupport/SideStoreClient.swift"
    if xpc_header.is_file() and xpc_objc.is_file() and ss_client.is_file():
        bridge_mod = _load_sibling_module("v3_refresh_result_bridge", "patch_refresh_result_bridge.py")
        for file_path, transform in (
            (xpc_header, bridge_mod.install_direct_host_header),
            (xpc_objc, bridge_mod.install_direct_host_objc),
            (ss_client, bridge_mod.install_direct_host_client),
        ):
            before = file_path.read_text(encoding="utf-8")
            after = transform(before)
            if after != before:
                file_path.write_text(after, encoding="utf-8")
                changed = True

    # 1. Patch SideStoreSupport/SideStore.swift
    text = side_store.read_text(encoding="utf-8")
    if "@MainActor\nclass RefreshHandler: NSObject {" in text and "enum V3RefreshCallbackIdentityPolicy" not in text:
        admission_mod = _load_sibling_module("v3_refresh_callback_admission", "patch_refresh_callback_admission.py")
        text = admission_mod.install_host(text)
        side_store.write_text(text, encoding="utf-8")
        changed = True
    if MARKER in text:
        pass
    else:
        if LEASE_V601_OLD in text:
            text = replace_once(text, LEASE_V601_OLD, LEASE_V601_NEW, "V3RefreshAdmissionLease in SideStore.swift")
        else:
            text = replace_once(text, LEASE_OLD, LEASE_NEW, "V3RefreshAdmissionLease in SideStore.swift")
        text = replace_once(text, SIDESTORE_SNAPSHOT_OLD, SIDESTORE_SNAPSHOT_NEW, "statusReplyMayApply in SideStore.swift")
        side_store.write_text(text, encoding="utf-8")
        changed = True

    # 2. Patch LiveContainerSwiftUI/Views/V3UnifiedShell.swift
    text_shell = v3_shell.read_text(encoding="utf-8")
    if MARKER not in text_shell:
        if LEASE_V601_OLD in text_shell:
            text_shell = replace_once(text_shell, LEASE_V601_OLD, LEASE_V601_NEW, "V3RefreshAdmissionLease in V3UnifiedShell.swift")
        else:
            text_shell = replace_once(text_shell, LEASE_OLD, LEASE_NEW, "V3RefreshAdmissionLease in V3UnifiedShell.swift")
        text_shell = replace_once(text_shell, ACCEPT_OLD, ACCEPT_NEW, "accept() in V3UnifiedShell.swift")
        text_shell = replace_once(text_shell, RECONCILE_OLD, RECONCILE_NEW, "reconcileLostRefreshAfterDeviceCheck in V3UnifiedShell.swift")
        text_shell = replace_once(text_shell, AUTO_RECONCILE_OBSERVE_OLD_1, AUTO_RECONCILE_OBSERVE_NEW_1, "acceptRecoveryEvidence refreshRecovery in V3UnifiedShell.swift")
        text_shell = replace_once(text_shell, AUTO_RECONCILE_OBSERVE_OLD_2, AUTO_RECONCILE_OBSERVE_NEW_2, "applyFullRecoveryEvidence refreshRecovery in V3UnifiedShell.swift")
        text_shell = replace_once(text_shell, BANNER_OLD, BANNER_NEW, "V3RecoveryBannerLayout in V3UnifiedShell.swift")
        text_shell = replace_once(text_shell, BANNER_VIEW_OLD, BANNER_VIEW_NEW, "unresolvedRefreshRecoveryRunID banner branch in V3UnifiedShell.swift")
        if PRESENT_ERROR_V601_OLD in text_shell:
            text_shell = replace_once(text_shell, PRESENT_ERROR_V601_OLD, PRESENT_ERROR_V601_NEW, "shouldMarkDisconnected error presentation in V3UnifiedShell.swift")
        else:
            text_shell = replace_once(text_shell, PRESENT_ERROR_OLD, PRESENT_ERROR_NEW, "shouldMarkDisconnected error presentation in V3UnifiedShell.swift")
        v3_shell.write_text(text_shell, encoding="utf-8")
        changed = True

    return changed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: patch_refresh_recovery_deadlock.py <path-to-LiveContainer>", file=sys.stderr)
        sys.exit(1)
    applied = patch_repository(Path(sys.argv[1]))
    print(f"Patch applied: {applied}")
