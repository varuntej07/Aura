#include "window_effects_channel.h"

#include <dwmapi.h>
#include <windows.h>

#include <flutter/encodable_value.h>
#include <flutter/method_channel.h>
#include <flutter/standard_method_codec.h>

#include <memory>

namespace {

// SetWindowCompositionAttribute is the undocumented-but-stable user32 call
// window_manager itself uses for the transparent background (see its
// SetBackgroundColor). ACCENT_ENABLE_TRANSPARENTGRADIENT is the ONLY accent
// state this file ever requests now (2026-07-10): earlier passes tried DWM's
// native Acrylic/Mica system backdrop (DWMWA_SYSTEMBACKDROP_TYPE) plus manual
// corner rounding (SetWindowRgn, then DWMWA_WINDOW_CORNER_PREFERENCE) to get
// a real blurred-glass look with custom-radius rounded corners. Neither
// rounding mechanism reliably shaped the window as one piece: SetWindowRgn
// clips the top-level window but not the Flutter child view's own
// DirectComposition surface (which composites independently of GDI region
// clipping), and DWMWA_WINDOW_CORNER_PREFERENCE only offers a small ~8px
// system radius with no public API for a larger custom value (confirmed via
// research — third-party Windows patchers exist specifically because there's
// no supported way past that cap). Both looked like "two concentric squares"
// in practice: the system backdrop paints to the window's full rectangular
// bounds, and no rounding attempt reliably clipped that.
//
// The fix: don't ask the OS to render a rounded, blurred window at all. Make
// the native window fully and permanently transparent (this state), and let
// _GlassSurface in overlay_panel.dart paint the ENTIRE visible card itself —
// fill, border, and rounding at any radius Flutter wants — the same
// "gradient + border, no real blur" pattern this codebase already uses for
// FauxGlassCard elsewhere. There is exactly one shape-drawing authority now,
// so there is nothing left to mismatch. The tradeoff is losing genuine
// blurred-desktop-behind-glass (Flutter's BackdropFilter can only blur what
// Flutter itself already painted, never the real desktop behind a
// transparent OS window — confirmed via research, not a Flutter bug).
enum AccentState {
  kAccentEnableTransparentGradient = 2,
};

// Removes WS_CAPTION/WS_THICKFRAME/WS_SYSMENU/WS_MINIMIZEBOX/WS_MAXIMIZEBOX
// so no titlebar, resizable border, or system menu can attach to the overlay,
// then forces DWM to recompute non-client rendering (SWP_FRAMECHANGED) so a
// frame/shadow cached from window creation can't linger. window_manager's
// setAsFrameless() already asks for this from the Dart side; doing it here
// too, natively and idempotently, means the overlay's borderless shape never
// depends on plugin call ordering.
void EnsureBorderlessStyle(HWND window) {
  const LONG_PTR style = GetWindowLongPtr(window, GWL_STYLE);
  const LONG_PTR borderless = style & ~(WS_CAPTION | WS_THICKFRAME |
                                        WS_SYSMENU | WS_MINIMIZEBOX |
                                        WS_MAXIMIZEBOX);
  if (borderless != style) {
    SetWindowLongPtr(window, GWL_STYLE, borderless);
  }
  SetWindowPos(window, nullptr, 0, 0, 0, 0,
              SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE |
                  SWP_FRAMECHANGED);
}

// DWMWA_WINDOW_CORNER_PREFERENCE / DWMWCP_DONOTROUND: explicitly tells DWM
// not to apply its own small system rounding to this window's chrome, since
// Flutter now owns 100% of the visible shape and any native rounding
// attempt (even the small automatic one) is a second, independent shape
// authority this app no longer wants.
constexpr DWORD kCornerPreferenceAttribute = 33;  // DWMWA_WINDOW_CORNER_PREFERENCE
constexpr int kCornerPreferenceDoNotRound = 1;     // DWMWCP_DONOTROUND

void ApplyCornerPreference(HWND window, int preference) {
  DwmSetWindowAttribute(window, kCornerPreferenceAttribute, &preference,
                        sizeof(preference));
}

// DWMWA_NCRENDERING_POLICY / DWMNCRP_DISABLED: tells DWM this window fully
// owns its own non-client look, no theme-drawn chrome to layer on top of it.
// Belt-and-suspenders alongside EnsureBorderlessStyle and the corner
// preference above — all three exist to guarantee nothing but Flutter's own
// paint is ever visible.
constexpr DWORD kNcRenderingPolicyAttribute = 2;  // DWMWA_NCRENDERING_POLICY
constexpr int kNcRenderingPolicyDisabled = 1;     // DWMNCRP_DISABLED

void ApplyNcRenderingPolicy(HWND window) {
  const int policy = kNcRenderingPolicyDisabled;
  DwmSetWindowAttribute(window, kNcRenderingPolicyAttribute, &policy,
                        sizeof(policy));
}

struct AccentPolicy {
  int accent_state;
  int flags;
  int gradient_color;  // AABBGGRR, not ARGB.
  int animation_id;
};

struct WindowCompositionAttributeData {
  int attribute;  // 19 = WCA_ACCENT_POLICY.
  PVOID data;
  ULONG data_size;
};

typedef BOOL(WINAPI* SetWindowCompositionAttributeFn)(
    HWND, WindowCompositionAttributeData*);

bool ApplyAccent(HWND window, int accent_state, int gradient_color) {
  const HMODULE user32 = LoadLibrary(TEXT("user32.dll"));
  if (!user32) {
    return false;
  }
  auto set_attribute = reinterpret_cast<SetWindowCompositionAttributeFn>(
      GetProcAddress(user32, "SetWindowCompositionAttribute"));
  bool applied = false;
  if (set_attribute) {
    AccentPolicy policy = {accent_state, 2, gradient_color, 0};
    WindowCompositionAttributeData data = {19, &policy, sizeof(policy)};
    applied = set_attribute(window, &data) != FALSE;
  }
  FreeLibrary(user32);
  return applied;
}

// Fully, permanently transparent: no OS backdrop, no OS rounding attempt.
// Called once at registration and again on every presentation change (matches
// the call pattern that already reliably worked pre-2026-07-09), since there
// is no longer an "enabled vs disabled" distinction — every presentation
// (panel, pill, and the fullscreen pointing flight) wants exactly this same
// native state; only Flutter's own painted content differs between them.
bool ApplyTransparentWindowState(HWND window) {
  EnsureBorderlessStyle(window);
  ApplyCornerPreference(window, kCornerPreferenceDoNotRound);
  ApplyNcRenderingPolicy(window);
  return ApplyAccent(window, kAccentEnableTransparentGradient, 0);
}

// SetForegroundWindow alone is DENIED while another process owns the
// foreground (the OS foreground lock), which is the overlay's normal case:
// the summon hotkey fires while the user works in another app. The window
// then sits visible but without keyboard focus — Esc lands in the other app
// and no blur event ever fires. Attaching this thread's input queue to the
// foreground thread's makes the OS treat both as one input context, so the
// handoff is permitted (the long-standing launcher/HUD pattern). ShowWindow
// runs first because window_manager shows via async ShowWindowAsync, and a
// still-hidden window can never take the foreground.
bool ForceForeground(HWND window) {
  ShowWindow(window, SW_SHOW);
  const HWND foreground = GetForegroundWindow();
  const DWORD this_thread = GetCurrentThreadId();
  DWORD foreground_thread = 0;
  if (foreground && foreground != window) {
    foreground_thread = GetWindowThreadProcessId(foreground, nullptr);
  }
  const bool attach =
      foreground_thread != 0 && foreground_thread != this_thread;
  if (attach) {
    AttachThreadInput(foreground_thread, this_thread, TRUE);
  }
  BringWindowToTop(window);
  SetForegroundWindow(window);
  // Keyboard focus belongs on the Flutter child view, not the frame window;
  // mirrors the WM_ACTIVATE handler in win32_window.cpp.
  const HWND child = GetWindow(window, GW_CHILD);
  SetFocus(child ? child : window);
  if (attach) {
    AttachThreadInput(foreground_thread, this_thread, FALSE);
  }
  return GetForegroundWindow() == window;
}

}  // namespace

void RegisterWindowEffectsChannel(flutter::BinaryMessenger* messenger,
                                  HWND window) {
  // Applied once at registration (window exists but isn't shown yet) so the
  // overlay is already borderless and transparent before the very first
  // ensureTransparent call, not dependent on it.
  ApplyTransparentWindowState(window);

  static auto channel =
      std::make_unique<flutter::MethodChannel<flutter::EncodableValue>>(
          messenger, "aura/window_effects",
          &flutter::StandardMethodCodec::GetInstance());

  channel->SetMethodCallHandler([window](const auto& call, auto result) {
    if (call.method_name() == "focusWindow") {
      result->Success(flutter::EncodableValue(ForceForeground(window)));
      return;
    }
    if (call.method_name() != "ensureTransparent") {
      result->NotImplemented();
      return;
    }
    result->Success(
        flutter::EncodableValue(ApplyTransparentWindowState(window)));
  });
}
