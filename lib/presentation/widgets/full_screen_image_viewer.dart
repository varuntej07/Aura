import 'dart:typed_data';

import 'package:flutter/material.dart';

/// Full-screen, zoomable preview of an attached image. Pushed as a translucent
/// route over the current screen. Pinch or double-drag to zoom and pan; tap the
/// backdrop or the close button to dismiss.
///
/// Renders the full-resolution [bytes] (not a compressed thumbnail), so callers
/// should pass `attachment.bytes`.
class FullScreenImageViewer extends StatelessWidget {
  final Uint8List bytes;

  const FullScreenImageViewer({super.key, required this.bytes});

  /// Opens the viewer over [context] with a fade transition.
  static Future<void> open(BuildContext context, {required Uint8List bytes}) {
    return Navigator.of(context, rootNavigator: true).push(
      PageRouteBuilder<void>(
        opaque: false,
        barrierColor: Colors.black,
        transitionDuration: const Duration(milliseconds: 200),
        reverseTransitionDuration: const Duration(milliseconds: 150),
        pageBuilder: (_, _, _) => FullScreenImageViewer(bytes: bytes),
        transitionsBuilder: (_, animation, _, child) =>
            FadeTransition(opacity: animation, child: child),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: Stack(
        children: [
          Positioned.fill(
            child: GestureDetector(
              onTap: () => Navigator.of(context).maybePop(),
              child: InteractiveViewer(
                minScale: 1.0,
                maxScale: 5.0,
                child: Center(
                  child: Image.memory(bytes, fit: BoxFit.contain),
                ),
              ),
            ),
          ),
          Positioned(
            top: 8,
            right: 8,
            child: SafeArea(
              child: IconButton(
                icon: const Icon(
                  Icons.close_rounded,
                  color: Colors.white,
                  size: 28,
                ),
                onPressed: () => Navigator.of(context).maybePop(),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
