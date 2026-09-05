import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:uuid/uuid.dart';

enum ChatAttachmentType { image, document }

/// In-memory only, never serialised to the local database.
/// Lives on [ChatMessageModel.attachments] for the lifetime of the VM session.
class ChatAttachment {
  final String id;
  final String fileName;
  final String mimeType;
  final int fileSizeBytes;
  final Uint8List bytes;
  final ChatAttachmentType type;
  // Compressed preview bytes used in thumbnail display (null for documents).
  final Uint8List? thumbnail;

  ChatAttachment({
    String? id,
    required this.fileName,
    required this.mimeType,
    required this.fileSizeBytes,
    required this.bytes,
    required this.type,
    this.thumbnail,
  }) : id = id ?? const Uuid().v4();

  // Base64 of [bytes], computed once per attachment and reused by every send.
  // Without the memo, the current turn AND up to three multimodal history turns
  // re-encoded multi-MB images on the UI isolate on every single send.
  String? _encodedData;

  String get encodedData => _encodedData ??= base64Encode(bytes);

  /// Pre-warms [encodedData] off the UI isolate. Safe to call repeatedly;
  /// later synchronous reads hit the memo.
  Future<String> encodeOffIsolate() async {
    final cached = _encodedData;
    if (cached != null) return cached;
    final encoded = await compute(base64Encode, bytes);
    _encodedData = encoded;
    return encoded;
  }

  Map<String, dynamic> toRequestPayload() => {
        'type': type.name,
        'mime_type': mimeType,
        'data': encodedData,
        'file_name': fileName,
      };
}
