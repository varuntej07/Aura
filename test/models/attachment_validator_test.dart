import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:aura/data/models/attachment_validator.dart';
import 'package:aura/data/models/chat_attachment.dart';

ChatAttachment _fakeAttachment({int sizeBytes = 1000, ChatAttachmentType type = ChatAttachmentType.image}) {
  return ChatAttachment(
    fileName: 'test.jpg',
    mimeType: 'image/jpeg',
    fileSizeBytes: sizeBytes,
    bytes: Uint8List(sizeBytes),
    type: type,
  );
}

void main() {
  group('AttachmentValidator.validate', () {
    test('accepts a valid JPEG image', () {
      final result = AttachmentValidator.validate(
        mimeType: 'image/jpeg',
        fileSizeBytes: 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isTrue);
      expect(result.resolvedType, ChatAttachmentType.image);
    });

    test('accepts a valid PNG image', () {
      final result = AttachmentValidator.validate(
        mimeType: 'image/png',
        fileSizeBytes: 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isTrue);
      expect(result.resolvedType, ChatAttachmentType.image);
    });

    test('accepts a valid PDF document', () {
      final result = AttachmentValidator.validate(
        mimeType: 'application/pdf',
        fileSizeBytes: 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isTrue);
      expect(result.resolvedType, ChatAttachmentType.document);
    });

    test('accepts a valid DOCX document', () {
      final result = AttachmentValidator.validate(
        mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        fileSizeBytes: 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isTrue);
      expect(result.resolvedType, ChatAttachmentType.document);
    });

    test('accepts a valid TXT document', () {
      final result = AttachmentValidator.validate(
        mimeType: 'text/plain',
        fileSizeBytes: 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isTrue);
      expect(result.resolvedType, ChatAttachmentType.document);
    });

    test('rejects unsupported MIME type', () {
      final result = AttachmentValidator.validate(
        mimeType: 'image/svg+xml',
        fileSizeBytes: 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isFalse);
      expect(result.error, AttachmentValidationError.unsupportedType);
    });

    test('rejects image over 5 MB', () {
      final result = AttachmentValidator.validate(
        mimeType: 'image/jpeg',
        fileSizeBytes: 5 * 1024 * 1024 + 1,
        existingAttachments: [],
      );
      expect(result.isValid, isFalse);
      expect(result.error, AttachmentValidationError.imageTooLarge);
    });

    test('accepts image exactly at 5 MB', () {
      final result = AttachmentValidator.validate(
        mimeType: 'image/jpeg',
        fileSizeBytes: 5 * 1024 * 1024,
        existingAttachments: [],
      );
      expect(result.isValid, isTrue);
    });

    test('rejects document over 10 MB', () {
      final result = AttachmentValidator.validate(
        mimeType: 'application/pdf',
        fileSizeBytes: 10 * 1024 * 1024 + 1,
        existingAttachments: [],
      );
      expect(result.isValid, isFalse);
      expect(result.error, AttachmentValidationError.documentTooLarge);
    });

    test('rejects when 5 attachments already exist', () {
      final existing = List.generate(5, (_) => _fakeAttachment());
      final result = AttachmentValidator.validate(
        mimeType: 'image/jpeg',
        fileSizeBytes: 1024,
        existingAttachments: existing,
      );
      expect(result.isValid, isFalse);
      expect(result.error, AttachmentValidationError.tooManyFiles);
    });

    test('accepts when 4 attachments exist', () {
      final existing = List.generate(4, (_) => _fakeAttachment());
      final result = AttachmentValidator.validate(
        mimeType: 'image/jpeg',
        fileSizeBytes: 1024,
        existingAttachments: existing,
      );
      expect(result.isValid, isTrue);
    });

    test('rejects when total payload exceeds 20 MB', () {
      final existing = [_fakeAttachment(sizeBytes: 19 * 1024 * 1024)];
      final result = AttachmentValidator.validate(
        mimeType: 'image/jpeg',
        fileSizeBytes: 2 * 1024 * 1024,
        existingAttachments: existing,
      );
      expect(result.isValid, isFalse);
      expect(result.error, AttachmentValidationError.totalPayloadTooLarge);
    });
  });

  group('AttachmentValidator.resolveType', () {
    test('resolves image MIME types', () {
      expect(AttachmentValidator.resolveType('image/jpeg'), ChatAttachmentType.image);
      expect(AttachmentValidator.resolveType('image/png'), ChatAttachmentType.image);
      expect(AttachmentValidator.resolveType('image/gif'), ChatAttachmentType.image);
      expect(AttachmentValidator.resolveType('image/webp'), ChatAttachmentType.image);
    });

    test('resolves document MIME types', () {
      expect(AttachmentValidator.resolveType('application/pdf'), ChatAttachmentType.document);
      expect(AttachmentValidator.resolveType('text/plain'), ChatAttachmentType.document);
    });

    test('returns null for unsupported MIME types', () {
      expect(AttachmentValidator.resolveType('image/svg+xml'), isNull);
      expect(AttachmentValidator.resolveType('application/zip'), isNull);
    });
  });

  group('AttachmentValidator.mimeTypeFromExtension', () {
    test('maps known extensions', () {
      expect(AttachmentValidator.mimeTypeFromExtension('jpg'), 'image/jpeg');
      expect(AttachmentValidator.mimeTypeFromExtension('jpeg'), 'image/jpeg');
      expect(AttachmentValidator.mimeTypeFromExtension('png'), 'image/png');
      expect(AttachmentValidator.mimeTypeFromExtension('pdf'), 'application/pdf');
      expect(AttachmentValidator.mimeTypeFromExtension('txt'), 'text/plain');
    });

    test('is case-insensitive', () {
      expect(AttachmentValidator.mimeTypeFromExtension('JPG'), 'image/jpeg');
      expect(AttachmentValidator.mimeTypeFromExtension('PDF'), 'application/pdf');
    });

    test('returns null for unknown extension', () {
      expect(AttachmentValidator.mimeTypeFromExtension('xyz'), isNull);
    });

    test('returns null for null input', () {
      expect(AttachmentValidator.mimeTypeFromExtension(null), isNull);
    });
  });

  group('AttachmentValidator.errorMessage', () {
    test('returns human-readable messages for all error types', () {
      for (final error in AttachmentValidationError.values) {
        expect(AttachmentValidator.errorMessage(error), isNotEmpty);
      }
    });
  });
}
