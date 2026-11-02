#!/usr/bin/env python3
"""Patch LiveContainer with dynamic orientation engine for guest apps.

Provides dynamic rotation support for video players (e.g. SenPlayer) and apps
when the user taps rotate/fullscreen buttons, even when iOS Control Center
'Portrait Orientation Lock' is active, without permanently locking LiveContainer
to landscape.
"""
from __future__ import annotations

from pathlib import Path
import sys

MARKER = "LC_GUEST_ORIENTATION_DYNAMIC_V1"

UIKIT_PRIVATE_REL = Path("LiveContainer/UIKitPrivate.h")
GUEST_HOOKS_REL = Path("TweakLoader/UIKit+GuestHooks.m")

UIKIT_PRIVATE_OLD = """@interface UIWindow (private)
- (void)setAutorotates:(BOOL)autorotates forceUpdateInterfaceOrientation:(BOOL)force;
@end"""

UIKIT_PRIVATE_NEW = """@interface UIWindow (private)
- (void)setAutorotates:(BOOL)autorotates forceUpdateInterfaceOrientation:(BOOL)force;
- (void)_forceUpdateInterfaceOrientationIncludingRetargeting:(BOOL)retarget;
@end"""

ORIENTATION_HELPERS = """// LC_GUEST_ORIENTATION_DYNAMIC_V1: dynamic orientation engine
static inline void safeSwizzle(Class class, SEL originalAction, SEL swizzledAction) {
    Method origMethod = class_getInstanceMethod(class, originalAction);
    Method swizzMethod = class_getInstanceMethod(class, swizzledAction);
    if (!origMethod || !swizzMethod) return;
    BOOL didAddMethod = class_addMethod(class,
                                        originalAction,
                                        method_getImplementation(swizzMethod),
                                        method_getTypeEncoding(swizzMethod));
    if (didAddMethod) {
        class_replaceMethod(class,
                            swizzledAction,
                            method_getImplementation(origMethod),
                            method_getTypeEncoding(origMethod));
    } else {
        method_exchangeImplementations(origMethod, swizzMethod);
    }
}

static inline void addOrSwizzleMethod(Class cls, SEL origSel, SEL swizzSel) {
    Method swizzMethod = class_getInstanceMethod(cls, swizzSel);
    if (!swizzMethod) return;
    Method origMethod = class_getInstanceMethod(cls, origSel);
    if (!origMethod) {
        class_addMethod(cls, origSel, method_getImplementation(swizzMethod), method_getTypeEncoding(swizzMethod));
    } else {
        safeSwizzle(cls, origSel, swizzSel);
    }
}

static UIInterfaceOrientation LCDynamicOrientation = UIInterfaceOrientationUnknown;
static BOOL LCDynamicOrientationActive = NO;

static void LCApplyDynamicOrientation(UIInterfaceOrientation orientation) {
    if (orientation == UIInterfaceOrientationUnknown) {
        LCDynamicOrientation = UIInterfaceOrientationUnknown;
        LCDynamicOrientationActive = NO;
    } else {
        LCDynamicOrientation = orientation;
        LCDynamicOrientationActive = YES;
    }

    void (^applyBlock)(void) = ^{
        [[NSNotificationCenter defaultCenter] postNotificationName:UIDeviceOrientationDidChangeNotification
                                                            object:UIDevice.currentDevice];

        for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
            if (![scene isKindOfClass:UIWindowScene.class]) continue;
            UIWindowScene *windowScene = (UIWindowScene *)scene;

            for (UIWindow *window in windowScene.windows) {
                UIViewController *rootVC = window.rootViewController;
                if ([rootVC respondsToSelector:@selector(setNeedsUpdateOfSupportedInterfaceOrientations)]) {
                    [rootVC setNeedsUpdateOfSupportedInterfaceOrientations];
                }

                if ([window respondsToSelector:@selector(setAutorotates:forceUpdateInterfaceOrientation:)]) {
                    [window setAutorotates:YES forceUpdateInterfaceOrientation:YES];
                }
                if ([window respondsToSelector:@selector(_forceUpdateInterfaceOrientationIncludingRetargeting:)]) {
                    [window _forceUpdateInterfaceOrientationIncludingRetargeting:YES];
                }
            }
        }

        [UIViewController attemptRotationToDeviceOrientation];
    };

    if ([NSThread isMainThread]) {
        applyBlock();
        dispatch_async(dispatch_get_main_queue(), applyBlock);
    } else {
        dispatch_async(dispatch_get_main_queue(), applyBlock);
    }
}
"""

INIT_HOOKS_OLD = """    swizzle(UIScene.class, @selector(openURL:options:completionHandler:), @selector(hook_openURL:options:completionHandler:));
    NSInteger LCOrientationLockDirection = [NSUserDefaults.guestAppInfo[@"LCOrientationLock"] integerValue];"""

INIT_HOOKS_NEW = """    swizzle(UIScene.class, @selector(openURL:options:completionHandler:), @selector(hook_openURL:options:completionHandler:));

    // LC_GUEST_ORIENTATION_DYNAMIC_V1: swizzle window, controller, device, scene & application hooks
    safeSwizzle(UIWindow.class, @selector(makeKeyAndVisible), @selector(hook_makeKeyAndVisible));
    safeSwizzle(UIWindow.class, @selector(makeKeyWindow), @selector(hook_makeKeyWindow));
    safeSwizzle(UIWindow.class, @selector(resignKeyWindow), @selector(hook_resignKeyWindow));
    safeSwizzle(UIWindow.class, @selector(setHidden:), @selector(hook_setHidden:));
    safeSwizzle(UIWindow.class, @selector(setAutorotates:forceUpdateInterfaceOrientation:), @selector(hook_setAutorotates:forceUpdateInterfaceOrientation:));

    addOrSwizzleMethod(UIViewController.class, @selector(supportedInterfaceOrientations), @selector(hook_supportedInterfaceOrientations));
    addOrSwizzleMethod(UIViewController.class, @selector(__supportedInterfaceOrientations), @selector(hook___supportedInterfaceOrientations));
    addOrSwizzleMethod(UIViewController.class, @selector(shouldAutorotate), @selector(hook_shouldAutorotate));
    addOrSwizzleMethod(UIViewController.class, @selector(preferredInterfaceOrientationForPresentation), @selector(hook_preferredInterfaceOrientationForPresentation));
    addOrSwizzleMethod(UIViewController.class, @selector(shouldAutorotateToInterfaceOrientation:), @selector(hook_shouldAutorotateToInterfaceOrientation:));
    safeSwizzle(UIViewController.class, @selector(viewDidAppear:), @selector(hook_viewDidAppear:));
    safeSwizzle(UIViewController.class, @selector(viewWillDisappear:), @selector(hook_viewWillDisappear:));

    safeSwizzle(UIDevice.class, @selector(orientation), @selector(hook_orientation));
    safeSwizzle(UIDevice.class, @selector(setValue:forKey:), @selector(hook_setValue:forKey:));
    addOrSwizzleMethod(UIDevice.class, @selector(setOrientation:animated:), @selector(hook_setOrientation:animated:));
    addOrSwizzleMethod(UIDevice.class, @selector(setOrientation:), @selector(hook_setOrientation:));

    if (@available(iOS 16.0, *)) {
        safeSwizzle(UIWindowScene.class, @selector(requestGeometryUpdateWithPreferences:errorHandler:), @selector(hook_requestGeometryUpdateWithPreferences:errorHandler:));
    }
    safeSwizzle(UIWindowScene.class, @selector(interfaceOrientation), @selector(hook_interfaceOrientation));
    safeSwizzle(UIApplication.class, @selector(statusBarOrientation), @selector(hook_statusBarOrientation));
    safeSwizzle(UIApplication.class, @selector(supportedInterfaceOrientationsForWindow:), @selector(hook_supportedInterfaceOrientationsForWindow:));

    NSInteger LCOrientationLockDirection = [NSUserDefaults.guestAppInfo[@"LCOrientationLock"] integerValue];"""

LEGACY_IMPLEMENTATIONS_OLD = """@implementation UIViewController(LiveContainerHook)

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
@end"""

DYNAMIC_IMPLEMENTATIONS_NEW = """// LC_GUEST_ORIENTATION_DYNAMIC_V1: implementation hooks
@implementation UIViewController (LiveContainerOrientationHook)
- (UIInterfaceOrientationMask)hook_supportedInterfaceOrientations {
    if (LCOrientationLock == UIInterfaceOrientationLandscapeRight) {
        return UIInterfaceOrientationMaskLandscape;
    } else if (LCOrientationLock == UIInterfaceOrientationPortrait) {
        return UIInterfaceOrientationMaskPortrait;
    }
    if (LCDynamicOrientationActive) {
        if (UIInterfaceOrientationIsLandscape(LCDynamicOrientation)) {
            return UIInterfaceOrientationMaskLandscape;
        } else if (UIInterfaceOrientationIsPortrait(LCDynamicOrientation)) {
            return UIInterfaceOrientationMaskPortrait;
        }
    }
    return [self hook_supportedInterfaceOrientations];
}

- (UIInterfaceOrientationMask)hook___supportedInterfaceOrientations {
    if (LCOrientationLock == UIInterfaceOrientationLandscapeRight) {
        return UIInterfaceOrientationMaskLandscape;
    } else if (LCOrientationLock == UIInterfaceOrientationPortrait) {
        return UIInterfaceOrientationMaskPortrait;
    }
    if (LCDynamicOrientationActive) {
        if (UIInterfaceOrientationIsLandscape(LCDynamicOrientation)) {
            return UIInterfaceOrientationMaskLandscape;
        } else if (UIInterfaceOrientationIsPortrait(LCDynamicOrientation)) {
            return UIInterfaceOrientationMaskPortrait;
        }
    }
    return [self hook___supportedInterfaceOrientations];
}

- (BOOL)hook_shouldAutorotate {
    if (LCOrientationLock != UIInterfaceOrientationUnknown || LCDynamicOrientationActive) {
        return YES;
    }
    return [self hook_shouldAutorotate];
}

- (UIInterfaceOrientation)hook_preferredInterfaceOrientationForPresentation {
    if (LCOrientationLock != UIInterfaceOrientationUnknown) {
        return LCOrientationLock;
    }
    if (LCDynamicOrientationActive && LCDynamicOrientation != UIInterfaceOrientationUnknown) {
        return LCDynamicOrientation;
    }
    return [self hook_preferredInterfaceOrientationForPresentation];
}

- (BOOL)hook_shouldAutorotateToInterfaceOrientation:(NSInteger)orientation {
    if (LCOrientationLock != UIInterfaceOrientationUnknown || LCDynamicOrientationActive) {
        return YES;
    }
    return [self hook_shouldAutorotateToInterfaceOrientation:orientation];
}

- (void)hook_viewDidAppear:(BOOL)animated {
    [self hook_viewDidAppear:animated];
    if (LCDynamicOrientationActive) {
        if ([self respondsToSelector:@selector(setNeedsUpdateOfSupportedInterfaceOrientations)]) {
            [self setNeedsUpdateOfSupportedInterfaceOrientations];
        }
    }
}

- (void)hook_viewWillDisappear:(BOOL)animated {
    [self hook_viewWillDisappear:animated];
    if ((self.isBeingDismissed || self.isMovingFromParentViewController) && LCDynamicOrientationActive) {
        LCApplyDynamicOrientation(UIInterfaceOrientationUnknown);
    }
}
@end

@implementation UIDevice (LiveContainerOrientationHook)
- (UIDeviceOrientation)hook_orientation {
    if (LCOrientationLock == UIInterfaceOrientationLandscapeRight) {
        return UIDeviceOrientationLandscapeLeft;
    } else if (LCOrientationLock == UIInterfaceOrientationPortrait) {
        return UIDeviceOrientationPortrait;
    }
    if (LCDynamicOrientationActive) {
        if (LCDynamicOrientation == UIInterfaceOrientationLandscapeRight) {
            return UIDeviceOrientationLandscapeLeft;
        } else if (LCDynamicOrientation == UIInterfaceOrientationLandscapeLeft) {
            return UIDeviceOrientationLandscapeRight;
        } else if (LCDynamicOrientation == UIInterfaceOrientationPortrait) {
            return UIDeviceOrientationPortrait;
        } else if (LCDynamicOrientation == UIInterfaceOrientationPortraitUpsideDown) {
            return UIDeviceOrientationPortraitUpsideDown;
        }
    }
    return [self hook_orientation];
}

- (void)hook_setValue:(id)value forKey:(NSString *)key {
    if ([key isEqualToString:@"orientation"]) {
        UIInterfaceOrientation orientation = (UIInterfaceOrientation)[value integerValue];
        switch (orientation) {
            case UIInterfaceOrientationUnknown:
                LCApplyDynamicOrientation(UIInterfaceOrientationUnknown);
                return;
            case UIInterfaceOrientationPortrait:
            case UIInterfaceOrientationPortraitUpsideDown:
            case UIInterfaceOrientationLandscapeLeft:
            case UIInterfaceOrientationLandscapeRight:
                LCApplyDynamicOrientation(orientation);
                return;
            default:
                break;
        }
    }
    [self hook_setValue:value forKey:key];
}

- (void)hook_setOrientation:(UIInterfaceOrientation)orientation animated:(BOOL)animated {
    LCApplyDynamicOrientation(orientation);
}

- (void)hook_setOrientation:(UIInterfaceOrientation)orientation {
    LCApplyDynamicOrientation(orientation);
}
@end

@implementation UIWindowScene (LiveContainerGeometryHook)
- (void)hook_requestGeometryUpdateWithPreferences:(id)preferences errorHandler:(void (^)(NSError *error))errorHandler {
    UIInterfaceOrientationMask targetMask = 0;
    @try {
        id val = [preferences valueForKey:@"interfaceOrientations"];
        if (val) {
            targetMask = (UIInterfaceOrientationMask)[val unsignedIntegerValue];
        }
    } @catch (NSException *e) {
    }

    BOOL hasLandscape = (targetMask & UIInterfaceOrientationMaskLandscape) != 0;
    BOOL hasPortrait = (targetMask & (UIInterfaceOrientationMaskPortrait | UIInterfaceOrientationMaskPortraitUpsideDown)) != 0;

    if (hasLandscape && !hasPortrait) {
        LCApplyDynamicOrientation(UIInterfaceOrientationLandscapeRight);
    } else if (hasPortrait && !hasLandscape) {
        LCApplyDynamicOrientation(UIInterfaceOrientationPortrait);
    }

    void (^wrappedErrorHandler)(NSError *) = ^(NSError *error) {
        if (error) {
            NSLog(@"[LC] System requestGeometryUpdate: %@ (handled dynamically by LiveContainer)", error.localizedDescription);
        }
        if (errorHandler) {
            errorHandler(error);
        }
    };
    [self hook_requestGeometryUpdateWithPreferences:preferences errorHandler:wrappedErrorHandler];
}

- (UIInterfaceOrientation)hook_interfaceOrientation {
    if (LCOrientationLock != UIInterfaceOrientationUnknown) {
        return LCOrientationLock;
    }
    if (LCDynamicOrientationActive && LCDynamicOrientation != UIInterfaceOrientationUnknown) {
        return LCDynamicOrientation;
    }
    return [self hook_interfaceOrientation];
}
@end

@implementation UIApplication (LiveContainerOrientationHook)
- (UIInterfaceOrientation)hook_statusBarOrientation {
    if (LCOrientationLock != UIInterfaceOrientationUnknown) {
        return LCOrientationLock;
    }
    if (LCDynamicOrientationActive && LCDynamicOrientation != UIInterfaceOrientationUnknown) {
        return LCDynamicOrientation;
    }
    return [self hook_statusBarOrientation];
}

- (UIInterfaceOrientationMask)hook_supportedInterfaceOrientationsForWindow:(UIWindow *)window {
    if (LCOrientationLock == UIInterfaceOrientationLandscapeRight) {
        return UIInterfaceOrientationMaskLandscape;
    } else if (LCOrientationLock == UIInterfaceOrientationPortrait) {
        return UIInterfaceOrientationMaskPortrait;
    }
    if (LCDynamicOrientationActive) {
        if (UIInterfaceOrientationIsLandscape(LCDynamicOrientation)) {
            return UIInterfaceOrientationMaskLandscape;
        } else if (UIInterfaceOrientationIsPortrait(LCDynamicOrientation)) {
            return UIInterfaceOrientationMaskPortrait;
        }
    }
    return [self hook_supportedInterfaceOrientationsForWindow:window];
}
@end

@implementation UIWindow (LiveContainerWindowHook)
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
@end"""


def die(message: str) -> None:
    raise SystemExit(f"patch_guest_orientation: {message}")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        die(f"{label}: expected one anchor, found {count}")
    return text.replace(old, new, 1)


def patch_uikit_private(live_root: Path) -> None:
    path = live_root / UIKIT_PRIVATE_REL
    if not path.is_file():
        die(f"UIKitPrivate.h not found at {path}")
    text = path.read_text(encoding="utf-8")
    if "- (void)_forceUpdateInterfaceOrientationIncludingRetargeting:(BOOL)retarget;" in text:
        return
    text = replace_once(text, UIKIT_PRIVATE_OLD, UIKIT_PRIVATE_NEW, "UIKitPrivate UIWindow _forceUpdateInterfaceOrientation")
    path.write_text(text, encoding="utf-8")


def patch_uikit_guest_hooks(live_root: Path) -> None:
    path = live_root / GUEST_HOOKS_REL
    if not path.is_file():
        die(f"UIKit+GuestHooks.m not found at {path}")
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        # Idempotency check: verify all markers are present
        if text.count(MARKER) != 3:
            die("previous dynamic orientation patch is partial or duplicated")
        return

    # 1. Insert helper methods & dynamic orientation engine near top
    anchor_top = "BOOL launchURLProcessed = NO;\n"
    if anchor_top not in text:
        die("top state anchor not found in UIKit+GuestHooks.m")
    text = replace_once(text, anchor_top, anchor_top + "\n" + ORIENTATION_HELPERS + "\n", "orientation helpers injection")

    # 2. Insert swizzle registration in UIKitGuestHooksInit()
    text = replace_once(text, INIT_HOOKS_OLD, INIT_HOOKS_NEW, "init hooks swizzling registration")

    # 3. Replace legacy implementations at bottom
    text = replace_once(text, LEGACY_IMPLEMENTATIONS_OLD, DYNAMIC_IMPLEMENTATIONS_NEW, "dynamic orientation implementations")

    path.write_text(text, encoding="utf-8")


def verify(live_root: Path) -> None:
    uikit_private_path = live_root / UIKIT_PRIVATE_REL
    guest_hooks_path = live_root / GUEST_HOOKS_REL

    if not uikit_private_path.is_file():
        die(f"UIKitPrivate.h missing at {uikit_private_path}")
    if not guest_hooks_path.is_file():
        die(f"UIKit+GuestHooks.m missing at {guest_hooks_path}")

    private_content = uikit_private_path.read_text(encoding="utf-8")
    if "_forceUpdateInterfaceOrientationIncludingRetargeting" not in private_content:
        die("UIKitPrivate.h missing _forceUpdateInterfaceOrientationIncludingRetargeting declaration")

    hooks_content = guest_hooks_path.read_text(encoding="utf-8")
    if MARKER not in hooks_content:
        die(f"UIKit+GuestHooks.m missing marker {MARKER}")
    if hooks_content.count(MARKER) != 3:
        die("UIKit+GuestHooks.m does not have exactly 3 marker occurrences")
    for symbol in (
        "LCApplyDynamicOrientation",
        "safeSwizzle",
        "addOrSwizzleMethod",
        "hook_requestGeometryUpdateWithPreferences",
        "hook_orientation",
        "hook_statusBarOrientation",
        "hook_supportedInterfaceOrientationsForWindow",
    ):
        if symbol not in hooks_content:
            die(f"UIKit+GuestHooks.m missing required symbol: {symbol}")


def patch_all(live_root: Path) -> None:
    patch_uikit_private(live_root)
    patch_uikit_guest_hooks(live_root)
    verify(live_root)


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 1:
        die("usage: patch_guest_orientation.py <livecontainer-root>")
    root = Path(argv[0]).resolve()
    patch_all(root)
    print(f"Successfully applied dynamic orientation engine patch to {root}")


if __name__ == "__main__":
    main()
