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

SIDESTORE_SNAPSHOT_NEW = """    public func statusReplyMayApply(_ reply: [String: Any]) -> Bool {
        guard let ticket = reply["_v3StatusAuthorityTicket"] as? V3StatusWriteTicket else { return false }
        let busyValue: Bool?
        if ticket.kind == .snapshot {
            let refreshLost: Bool = {
                if let refresh = reply["refreshRecovery"] as? [String: Any],
                   V3WireContract.strictBool(refresh["ownerLost"]) == true { return true }
                if let opRec = reply["operationRecovery"] as? [String: Any],
                   opRec["kind"] as? String == "refreshAll" { return true }
                return false
            }()
            let rawActive = V3WireContract.strictBool(reply["activeMutation"])
            let effectiveActiveMutation = refreshLost ? false : (rawActive == true)
            guard let typedBusy = V3WireContract.strictBool(reply["busy"]),
                  effectiveActiveMutation == false else { return false }
            // These ownership facts are emitted on every current service
            // snapshot. Missing or malformed fields cannot authorize a commit.
            guard rawActive != nil,
                  V3WireContract.strictBool(reply["recoveryHold"]) != nil else { return false }
            let recoveryOnly = V3RecoveryOnlySnapshotPolicy.mayApplyFullStatus(
                busy: typedBusy,
                activeMutation: effectiveActiveMutation,
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
        let refreshLost: Bool = {
            if let refresh = reply["refreshRecovery"] as? [String: Any],
               V3WireContract.strictBool(refresh["ownerLost"]) == true { return true }
            if let opRec = reply["operationRecovery"] as? [String: Any],
               opRec["kind"] as? String == "refreshAll" { return true }
            return false
        }()
        let hasActiveMutationField = reply.keys.contains("activeMutation")
        let activeMutation = refreshLost ? false : (V3WireContract.strictBool(reply["activeMutation"]) ?? true)
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
            return false
        }"""


def patch_repository(target_dir: Path) -> bool:
    side_store = target_dir / "SideStoreSupport/SideStore.swift"
    v3_shell = target_dir / "LiveContainerSwiftUI/Views/V3UnifiedShell.swift"

    if not side_store.is_file():
        raise FileNotFoundError(f"Missing SideStore.swift at {side_store}")
    if not v3_shell.is_file():
        raise FileNotFoundError(f"Missing V3UnifiedShell.swift at {v3_shell}")

    changed = False

    # 1. Patch SideStoreSupport/SideStore.swift
    content_ss = side_store.read_text(encoding="utf-8")
    if MARKER not in content_ss:
        if LEASE_OLD not in content_ss:
            raise ValueError("Could not find V3RefreshAdmissionLease anchor in SideStore.swift")
        if SIDESTORE_SNAPSHOT_OLD not in content_ss:
            raise ValueError("Could not find statusReplyMayApply anchor in SideStore.swift")
        content_ss = content_ss.replace(LEASE_OLD, LEASE_NEW, 1)
        content_ss = content_ss.replace(SIDESTORE_SNAPSHOT_OLD, SIDESTORE_SNAPSHOT_NEW, 1)
        side_store.write_text(content_ss, encoding="utf-8")
        changed = True

    # 2. Patch LiveContainerSwiftUI/Views/V3UnifiedShell.swift
    content_shell = v3_shell.read_text(encoding="utf-8")
    if MARKER not in content_shell:
        if LEASE_OLD not in content_shell:
            raise ValueError("Could not find V3RefreshAdmissionLease anchor in V3UnifiedShell.swift")
        if ACCEPT_OLD not in content_shell:
            raise ValueError("Could not find accept() anchor in V3UnifiedShell.swift")
        content_shell = content_shell.replace(LEASE_OLD, LEASE_NEW, 1)
        content_shell = content_shell.replace(ACCEPT_OLD, ACCEPT_NEW, 1)
        v3_shell.write_text(content_shell, encoding="utf-8")
        changed = True

    return changed


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: patch_refresh_recovery_deadlock.py <path-to-LiveContainer>", file=sys.stderr)
        sys.exit(1)
    applied = patch_repository(Path(sys.argv[1]))
    print(f"Patch applied: {applied}")
