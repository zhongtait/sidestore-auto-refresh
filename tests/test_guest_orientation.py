"""Unit tests for guest dynamic orientation patch (scripts/patch_guest_orientation.py).

Verifies that the patch correctly hooks UIWindow, UIViewController, UIDevice,
UIWindowScene, and UIApplication, ensures idempotency, and validates fail-closed
behavior on drift.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "guest_orientation_patch", ROOT / "scripts/patch_guest_orientation.py"
)
patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patch)

FIXTURE_UIKIT_PRIVATE = """#import <UIKit/UIKit.h>

@interface UIWindow (private)
- (void)setAutorotates:(BOOL)autorotates forceUpdateInterfaceOrientation:(BOOL)force;
@end

@interface _LSOpenConfiguration : NSObject
@property (copy, nonatomic) NSDictionary *frontBoardOptions;
@end
"""

FIXTURE_GUEST_HOOKS = """@import UIKit;
#import "LCSharedUtils.h"
#import "UIKitPrivate.h"
#import "../LiveContainer/utils.h"
#import <LocalAuthentication/LocalAuthentication.h>
#import "Localization.h"

UIInterfaceOrientation LCOrientationLock = UIInterfaceOrientationUnknown;
NSMutableArray<NSString*>* LCSupportedUrlSchemes = nil;
BOOL launchURLProcessed = NO;

__attribute__((constructor))
static void UIKitGuestHooksInit() {
    if(!NSUserDefaults.lcGuestAppId) return;
    swizzle(UIApplication.class, @selector(_applicationOpenURLAction:payload:origin:), @selector(hook__applicationOpenURLAction:payload:origin:));
    swizzle(UIApplication.class, @selector(_connectUISceneFromFBSScene:transitionContext:), @selector(hook__connectUISceneFromFBSScene:transitionContext:));
    swizzle(UIApplication.class, @selector(openURL:options:completionHandler:), @selector(hook_openURL:options:completionHandler:));
    swizzle(UIApplication.class, @selector(canOpenURL:), @selector(hook_canOpenURL:));
    swizzle(UIApplication.class, @selector(setDelegate:), @selector(hook_setDelegate:));
    swizzle(UIScene.class, @selector(scene:didReceiveActions:fromTransitionContext:), @selector(hook_scene:didReceiveActions:fromTransitionContext:));
    swizzle(UIScene.class, @selector(openURL:options:completionHandler:), @selector(hook_openURL:options:completionHandler:));
    NSInteger LCOrientationLockDirection = [NSUserDefaults.guestAppInfo[@"LCOrientationLock"] integerValue];
    if(LCOrientationLockDirection != 0 && [UIDevice.currentDevice userInterfaceIdiom] == UIUserInterfaceIdiomPhone) {
        switch (LCOrientationLockDirection) {
            case 1:
                LCOrientationLock = UIInterfaceOrientationLandscapeRight;
                break;
            case 2:
                LCOrientationLock = UIInterfaceOrientationPortrait;
                break;
            default:
                break;
        }
        if(!NSUserDefaults.isLiveProcess && LCOrientationLock != UIInterfaceOrientationUnknown) {
            swizzle(FBSSceneParameters.class, @selector(initWithXPCDictionary:), @selector(hook_initWithXPCDictionary:));
            swizzle(UIViewController.class, @selector(__supportedInterfaceOrientations), @selector(hook___supportedInterfaceOrientations));
            swizzle(UIViewController.class, @selector(shouldAutorotateToInterfaceOrientation:), @selector(hook_shouldAutorotateToInterfaceOrientation:));
            swizzle(UIWindow.class, @selector(setAutorotates:forceUpdateInterfaceOrientation:), @selector(hook_setAutorotates:forceUpdateInterfaceOrientation:));
        }

    }
}

@implementation UIViewController(LiveContainerHook)

- (UIInterfaceOrientationMask)hook___supportedInterfaceOrientations {
    if(LCOrientationLock == UIInterfaceOrientationLandscapeRight) {
        return UIInterfaceOrientationMaskLandscape;
    } else {
        return UIInterfaceOrientationMaskPortrait;
    }

}

- (BOOL)hook_shouldAutorotateToInterfaceOrientation:(NSInteger)orientation {
    return YES;
}

@end

@implementation UIWindow(hook)
- (void)hook_setAutorotates:(BOOL)autorotates forceUpdateInterfaceOrientation:(BOOL)force {
    [self hook_setAutorotates:YES forceUpdateInterfaceOrientation:YES];
}

- (void)hook_makeKeyAndVisible {
    [self updateWindowScene];
    [self hook_makeKeyAndVisible];
}
- (void)hook_makeKeyWindow {
    [self updateWindowScene];
    [self hook_makeKeyWindow];
}
- (void)hook_resignKeyWindow {
    [self updateWindowScene];
    [self hook_resignKeyWindow];
}
- (void)hook_setHidden:(BOOL)hidden {
    [self updateWindowScene];
    [self hook_setHidden:hidden];
}
- (void)updateWindowScene {
    for(UIWindowScene *windowScene in UIApplication.sharedApplication.connectedScenes) {
        if(!self.windowScene && self.screen == windowScene.screen) {
            self.windowScene = windowScene;
            break;
        }
    }
}
@end
"""


class GuestOrientationPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "LiveContainer").mkdir(parents=True, exist_ok=True)
        (self.root / "TweakLoader").mkdir(parents=True, exist_ok=True)

        self.uikit_private = self.root / "LiveContainer" / "UIKitPrivate.h"
        self.guest_hooks = self.root / "TweakLoader" / "UIKit+GuestHooks.m"

        self.uikit_private.write_text(FIXTURE_UIKIT_PRIVATE, encoding="utf-8")
        self.guest_hooks.write_text(FIXTURE_GUEST_HOOKS, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_patch_clean_tree(self):
        patch.patch_all(self.root)

        # Check UIKitPrivate.h
        private_content = self.uikit_private.read_text(encoding="utf-8")
        self.assertIn("- (void)_forceUpdateInterfaceOrientationIncludingRetargeting:(BOOL)retarget;", private_content)

        # Check UIKit+GuestHooks.m
        hooks_content = self.guest_hooks.read_text(encoding="utf-8")
        self.assertIn(patch.MARKER, hooks_content)
        self.assertEqual(hooks_content.count(patch.MARKER), 3)

        # Check core functions
        self.assertIn("static void LCApplyDynamicOrientation(UIInterfaceOrientation orientation)", hooks_content)
        self.assertIn("safeSwizzle(UIWindowScene.class, @selector(requestGeometryUpdateWithPreferences:errorHandler:)", hooks_content)
        self.assertIn("addOrSwizzleMethod(UIDevice.class, @selector(setOrientation:)", hooks_content)
        self.assertIn("safeSwizzle(UIApplication.class, @selector(supportedInterfaceOrientationsForWindow:)", hooks_content)

        # Verify passes
        patch.verify(self.root)

    def test_idempotency(self):
        patch.patch_all(self.root)
        first_private = self.uikit_private.read_text(encoding="utf-8")
        first_hooks = self.guest_hooks.read_text(encoding="utf-8")

        # Second patch execution should not change files or duplicate symbols
        patch.patch_all(self.root)
        second_private = self.uikit_private.read_text(encoding="utf-8")
        second_hooks = self.guest_hooks.read_text(encoding="utf-8")

        self.assertEqual(first_private, second_private)
        self.assertEqual(first_hooks, second_hooks)
        self.assertEqual(second_hooks.count(patch.MARKER), 3)

    def test_verify_fails_on_missing_symbol(self):
        patch.patch_all(self.root)

        # Remove a required symbol and verify failure
        corrupted = self.guest_hooks.read_text(encoding="utf-8").replace("LCApplyDynamicOrientation", "CorruptedSymbol")
        self.guest_hooks.write_text(corrupted, encoding="utf-8")

        with self.assertRaises(SystemExit):
            patch.verify(self.root)

    def test_fails_on_drifted_anchor(self):
        # Corrupt anchor in guest hooks
        corrupted = FIXTURE_GUEST_HOOKS.replace("BOOL launchURLProcessed = NO;\n", "BOOL launchURLProcessed = YES;\n")
        self.guest_hooks.write_text(corrupted, encoding="utf-8")

        with self.assertRaises(SystemExit):
            patch.patch_all(self.root)

    def test_cli_invocation(self):
        script_path = ROOT / "scripts" / "patch_guest_orientation.py"
        result = subprocess.run([sys.executable, str(script_path), str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, f"CLI invocation failed: {result.stderr}")
        self.assertIn("Successfully applied dynamic orientation engine patch", result.stdout)


if __name__ == "__main__":
    unittest.main()
