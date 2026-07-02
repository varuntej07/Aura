#ifndef RUNNER_SCREEN_CAPTURE_CHANNEL_H_
#define RUNNER_SCREEN_CAPTURE_CHANNEL_H_

#include <flutter/binary_messenger.h>

// Registers the "aura/screen_capture" method channel used by Buddy's screen
// sight: captures the display the cursor is on as a downscaled JPEG, entirely
// in native code (GDI capture + GDI+ encode), so Dart never touches raw pixels.
void RegisterScreenCaptureChannel(flutter::BinaryMessenger* messenger);

#endif  // RUNNER_SCREEN_CAPTURE_CHANNEL_H_
