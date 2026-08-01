import 'dart:convert';
import 'dart:typed_data';

import 'package:file_selector/file_selector.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/services/attachment_processor.dart';

void main() {
  group('AttachmentProcessor.processSelectedFile', () {
    test('creates a document attachment from a selected text file', () async {
      final processor = AttachmentProcessor();
      final file = XFile.fromData(
        utf8.encode('A short note.'),
        path: 'note.txt',
        name: 'note.txt',
        mimeType: 'text/plain',
      );

      final result = await processor.processSelectedFile(file, const []);

      expect(result.error, isNull);
      expect(result.attachment?.fileName, 'note.txt');
      expect(result.attachment?.mimeType, 'text/plain');
      expect(utf8.decode(result.attachment!.bytes), 'A short note.');
    });

    test('rejects a selected file with an unsupported extension', () async {
      final processor = AttachmentProcessor();
      final file = XFile.fromData(
        Uint8List.fromList(const [1, 2, 3]),
        path: 'unsafe.exe',
        name: 'unsafe.exe',
        mimeType: 'application/octet-stream',
      );

      final result = await processor.processSelectedFile(file, const []);

      expect(result.attachment, isNull);
      expect(result.error, contains('Format not supported'));
    });
  });
}
