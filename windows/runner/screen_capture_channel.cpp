#include "screen_capture_channel.h"

#include <windows.h>

#include <gdiplus.h>
#include <shellscalingapi.h>

#include <flutter/encodable_value.h>
#include <flutter/method_channel.h>
#include <flutter/standard_method_codec.h>

#include <algorithm>
#include <cmath>
#include <memory>
#include <optional>
#include <vector>

namespace {

// Long-edge ceiling for the encoded JPEG. ~1280 keeps a frame at roughly
// 100-300KB, which transfers over the LiveKit byte stream well under a second
// and stays close to the resolutions Anthropic's vision models see natively.
constexpr int kMaxJpegLongEdgePx = 1280;
constexpr LONG kJpegQuality = 70;

// GDI+ startup is process-wide and one-shot; the token is reclaimed by the OS
// at process teardown, so no matching shutdown is needed.
bool EnsureGdiplusStarted() {
  static bool started = [] {
    static ULONG_PTR token = 0;
    Gdiplus::GdiplusStartupInput input;
    return Gdiplus::GdiplusStartup(&token, &input, nullptr) == Gdiplus::Ok;
  }();
  return started;
}

bool GetJpegEncoderClsid(CLSID* clsid) {
  UINT count = 0;
  UINT size = 0;
  if (Gdiplus::GetImageEncodersSize(&count, &size) != Gdiplus::Ok || size == 0) {
    return false;
  }
  std::vector<BYTE> buffer(size);
  auto* codecs = reinterpret_cast<Gdiplus::ImageCodecInfo*>(buffer.data());
  if (Gdiplus::GetImageEncoders(count, size, codecs) != Gdiplus::Ok) {
    return false;
  }
  for (UINT i = 0; i < count; ++i) {
    if (wcscmp(codecs[i].MimeType, L"image/jpeg") == 0) {
      *clsid = codecs[i].Clsid;
      return true;
    }
  }
  return false;
}

std::optional<std::vector<uint8_t>> EncodeBitmapAsJpeg(HBITMAP bitmap) {
  CLSID jpeg_clsid;
  if (!GetJpegEncoderClsid(&jpeg_clsid)) {
    return std::nullopt;
  }

  Gdiplus::Bitmap gdiplus_bitmap(bitmap, nullptr);
  if (gdiplus_bitmap.GetLastStatus() != Gdiplus::Ok) {
    return std::nullopt;
  }

  IStream* stream = nullptr;
  if (CreateStreamOnHGlobal(nullptr, TRUE, &stream) != S_OK) {
    return std::nullopt;
  }

  LONG quality = kJpegQuality;
  Gdiplus::EncoderParameters encoder_params;
  encoder_params.Count = 1;
  encoder_params.Parameter[0].Guid = Gdiplus::EncoderQuality;
  encoder_params.Parameter[0].Type = Gdiplus::EncoderParameterValueTypeLong;
  encoder_params.Parameter[0].NumberOfValues = 1;
  encoder_params.Parameter[0].Value = &quality;

  std::optional<std::vector<uint8_t>> encoded;
  if (gdiplus_bitmap.Save(stream, &jpeg_clsid, &encoder_params) == Gdiplus::Ok) {
    HGLOBAL hglobal = nullptr;
    if (GetHGlobalFromStream(stream, &hglobal) == S_OK) {
      const SIZE_T size = GlobalSize(hglobal);
      if (void* data = GlobalLock(hglobal)) {
        const auto* bytes = static_cast<const uint8_t*>(data);
        encoded = std::vector<uint8_t>(bytes, bytes + size);
        GlobalUnlock(hglobal);
      }
    }
  }
  stream->Release();
  return encoded;
}

// Captures the monitor the cursor is on: physical-pixel StretchBlt into a
// downscaled bitmap, JPEG-encoded. Returns the Flutter map or nullopt.
std::optional<flutter::EncodableMap> CaptureCursorDisplay() {
  if (!EnsureGdiplusStarted()) {
    return std::nullopt;
  }

  POINT cursor{};
  GetCursorPos(&cursor);
  HMONITOR monitor = MonitorFromPoint(cursor, MONITOR_DEFAULTTONEAREST);
  MONITORINFO monitor_info{};
  monitor_info.cbSize = sizeof(MONITORINFO);
  if (!GetMonitorInfo(monitor, &monitor_info)) {
    return std::nullopt;
  }
  // rcMonitor is physical pixels in virtual-desktop coordinates: exactly the
  // space the pointing feature maps model coordinates back into.
  const RECT rc = monitor_info.rcMonitor;
  const int monitor_width = rc.right - rc.left;
  const int monitor_height = rc.bottom - rc.top;
  if (monitor_width <= 0 || monitor_height <= 0) {
    return std::nullopt;
  }

  UINT dpi_x = 96;
  UINT dpi_y = 96;
  GetDpiForMonitor(monitor, MDT_EFFECTIVE_DPI, &dpi_x, &dpi_y);
  const double scale_factor = dpi_x / 96.0;

  const double downscale = (std::min)(
      1.0, static_cast<double>(kMaxJpegLongEdgePx) /
               (std::max)(monitor_width, monitor_height));
  const int jpeg_width =
      (std::max)(1, static_cast<int>(std::lround(monitor_width * downscale)));
  const int jpeg_height =
      (std::max)(1, static_cast<int>(std::lround(monitor_height * downscale)));

  HDC screen_dc = GetDC(nullptr);
  if (!screen_dc) {
    return std::nullopt;
  }
  std::optional<flutter::EncodableMap> capture_result;
  HDC memory_dc = CreateCompatibleDC(screen_dc);
  if (memory_dc) {
    HBITMAP bitmap = CreateCompatibleBitmap(screen_dc, jpeg_width, jpeg_height);
    if (bitmap) {
      HGDIOBJ previous = SelectObject(memory_dc, bitmap);
      // HALFTONE gives a readable downscale (text stays legible for the
      // vision model); the default COLORONCOLOR drops whole pixel rows.
      SetStretchBltMode(memory_dc, HALFTONE);
      SetBrushOrgEx(memory_dc, 0, 0, nullptr);
      const BOOL blitted =
          StretchBlt(memory_dc, 0, 0, jpeg_width, jpeg_height, screen_dc,
                     rc.left, rc.top, monitor_width, monitor_height, SRCCOPY);
      SelectObject(memory_dc, previous);
      if (blitted) {
        if (auto jpeg = EncodeBitmapAsJpeg(bitmap)) {
          capture_result = flutter::EncodableMap{
              {flutter::EncodableValue("jpeg_bytes"),
               flutter::EncodableValue(std::move(*jpeg))},
              {flutter::EncodableValue("monitor_left_px"),
               flutter::EncodableValue(static_cast<int>(rc.left))},
              {flutter::EncodableValue("monitor_top_px"),
               flutter::EncodableValue(static_cast<int>(rc.top))},
              {flutter::EncodableValue("monitor_width_px"),
               flutter::EncodableValue(monitor_width)},
              {flutter::EncodableValue("monitor_height_px"),
               flutter::EncodableValue(monitor_height)},
              {flutter::EncodableValue("scale_factor"),
               flutter::EncodableValue(scale_factor)},
              {flutter::EncodableValue("jpeg_width_px"),
               flutter::EncodableValue(jpeg_width)},
              {flutter::EncodableValue("jpeg_height_px"),
               flutter::EncodableValue(jpeg_height)},
          };
        }
      }
      DeleteObject(bitmap);
    }
    DeleteDC(memory_dc);
  }
  ReleaseDC(nullptr, screen_dc);
  return capture_result;
}

}  // namespace

void RegisterScreenCaptureChannel(flutter::BinaryMessenger* messenger) {
  // Static so the channel (and its handler) outlives this function; the
  // messenger belongs to the engine, which outlives every capture call.
  static auto channel =
      std::make_unique<flutter::MethodChannel<flutter::EncodableValue>>(
          messenger, "aura/screen_capture",
          &flutter::StandardMethodCodec::GetInstance());

  channel->SetMethodCallHandler(
      [](const flutter::MethodCall<flutter::EncodableValue>& call,
         std::unique_ptr<flutter::MethodResult<flutter::EncodableValue>>
             result) {
        if (call.method_name() != "captureCursorDisplay") {
          result->NotImplemented();
          return;
        }
        if (auto capture = CaptureCursorDisplay()) {
          result->Success(flutter::EncodableValue(std::move(*capture)));
        } else {
          result->Error("capture_failed",
                        "Could not capture the display under the cursor.");
        }
      });
}
