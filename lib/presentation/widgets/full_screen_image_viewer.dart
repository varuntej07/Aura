import 'dart:typed_data';

import 'package:flutter/material.dart';

/// Zoomable preview of an attached image, shown as a card over a dimmed backdrop
/// occupying ~3/4 of the screen (not edge-to-edge). Pinch or double-drag to zoom
/// and pan; tap the backdrop or the close button to dismiss.
///
/// Renders the full-resolution [bytes] (not a compressed thumbnail), so callers
/// should pass `attachment.bytes`.
class FullScreenImageViewer extends StatelessWidget {
  /// Fraction of the screen the preview card occupies along each axis.
  static const double _cardWidthFraction = 0.88;
  static const double _cardHeightFraction = 0.74;

  final Uint8List bytes;

  const FullScreenImageViewer({super.key, required this.bytes});

  /// Opens the viewer over [context] with a fade transition.
  static Future<void> open(BuildContext context, {required Uint8List bytes}) {
    return Navigator.of(context, rootNavigator: true).push(
      PageRouteBuilder<void>(
        opaque: false,
        // Semi-transparent so the chat stays faintly visible around the card,
        // reading as a focused modal rather than a full-screen takeover.
        barrierColor: Colors.black.withValues(alpha: 0.78),
        transitionDuration: const Duration(milliseconds: 200),
        reverseTransitionDuration: const Duration(milliseconds: 150),
        pageBuilder: (_, _, _) => FullScreenImageViewer(bytes: bytes),
        transitionsBuilder: (_, animation, _, child) => FadeTransition(opacity: animation, child: child),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.of(context).size;
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: Stack(
        children: [
          // Tap anywhere on the dimmed backdrop to dismiss.
          Positioned.fill(
            child: GestureDetector(
              onTap: () => Navigator.of(context).maybePop(),
              behavior: HitTestBehavior.opaque,
            ),
          ),
          Center(
            child: SizedBox(
              width: size.width * _cardWidthFraction,
              height: size.height * _cardHeightFraction,
              child: Stack(
                clipBehavior: Clip.none,
                children: [
                  // The image card. Taps inside zoom/pan, they do not dismiss.
                  ClipRRect(
                    borderRadius: BorderRadius.circular(16),
                    child: InteractiveViewer(
                      minScale: 1.0,
                      maxScale: 5.0,
                      child: Center(
                        child: Image.memory(bytes, fit: BoxFit.contain),
                      ),
                    ),
                  ),
                  // Close button, top-right of the card.
                  Positioned(
                    top: 8,
                    right: 8,
                    child: GestureDetector(
                      onTap: () => Navigator.of(context).maybePop(),
                      child: Container(
                        decoration: const BoxDecoration(
                          color: Colors.black54,
                          shape: BoxShape.circle,
                        ),
                        padding: const EdgeInsets.all(6),
                        child: const Icon(
                          Icons.close_rounded,
                          color: Colors.white,
                          size: 22,
                        ),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
