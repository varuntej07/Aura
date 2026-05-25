import 'dart:convert';
import 'dart:typed_data';

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

  Map<String, dynamic> toRequestPayload() => {
        'type': type.name,
        'mime_type': mimeType,
        'data': base64Encode(bytes),
        'file_name': fileName,
      };
}
