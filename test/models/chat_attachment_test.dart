import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:aura/data/models/chat_attachment.dart';

void main() {
  group('ChatAttachment.toRequestPayload', () {
    test('encodes image bytes as base64', () {
      final bytes = Uint8List.fromList([0xFF, 0xD8, 0xFF, 0xE0]);
      final attachment = ChatAttachment(
        fileName: 'photo.jpg',
        mimeType: 'image/jpeg',
        fileSizeBytes: bytes.length,
        bytes: bytes,
        type: ChatAttachmentType.image,
      );

      final payload = attachment.toRequestPayload();

      expect(payload['type'], 'image');
      expect(payload['mime_type'], 'image/jpeg');
      expect(payload['file_name'], 'photo.jpg');
      expect(payload['data'], base64Encode(bytes));
    });

    test('encodes document bytes as base64', () {
      final bytes = Uint8List.fromList([0x25, 0x50, 0x44, 0x46]);
      final attachment = ChatAttachment(
        fileName: 'doc.pdf',
        mimeType: 'application/pdf',
        fileSizeBytes: bytes.length,
        bytes: bytes,
        type: ChatAttachmentType.document,
      );

      final payload = attachment.toRequestPayload();

      expect(payload['type'], 'document');
      expect(payload['mime_type'], 'application/pdf');
      expect(payload['data'], base64Encode(bytes));
    });

    test('generates unique IDs', () {
      final a = ChatAttachment(
        fileName: 'a.jpg',
        mimeType: 'image/jpeg',
        fileSizeBytes: 0,
        bytes: Uint8List(0),
        type: ChatAttachmentType.image,
      );
      final b = ChatAttachment(
        fileName: 'b.jpg',
        mimeType: 'image/jpeg',
        fileSizeBytes: 0,
        bytes: Uint8List(0),
        type: ChatAttachmentType.image,
      );
      expect(a.id, isNot(equals(b.id)));
    });
  });
}
