#ifndef RUNNER_WINDOW_EFFECTS_CHANNEL_H_
#define RUNNER_WINDOW_EFFECTS_CHANNEL_H_

#include <flutter/binary_messenger.h>
#include <windows.h>

// Registers the "aura/window_effects" method channel: keeps the top-level
// window permanently borderless and fully transparent (no OS backdrop, no OS
// corner rounding) via "ensureTransparent" — see the top-of-file comment in
// window_effects_channel.cpp for why native blur/rounding was dropped
// entirely in favor of Flutter painting the whole visible card itself.
//
// Also exposes "focusWindow": forces the overlay to the foreground with the
// AttachThreadInput handshake, because a bare SetForegroundWindow is denied
// while another process owns the foreground (the hotkey-summon case).
void RegisterWindowEffectsChannel(flutter::BinaryMessenger* messenger,
                                  HWND window);

#endif  // RUNNER_WINDOW_EFFECTS_CHANNEL_H_
