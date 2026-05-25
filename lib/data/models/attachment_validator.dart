import 'chat_attachment.dart';

const int _maxAttachmentsPerMessage = 5;
const int _maxImageSizeBytes = 5 * 1024 * 1024; // 5 MB — Anthropic hard limit
const int _maxDocumentSizeBytes = 10 * 1024 * 1024; // 10 MB
const int _maxTotalPayloadBytes = 20 * 1024 * 1024; // 20 MB raw —> ~27 MB after base64, under Cloud Run's 32 MB limit

// Kept in sync with backend/src/handlers/chat.py and lib/data/services/attachment_processor.dart
const Set<String> _supportedImageMimeTypes = {
  'image/jpeg',
  'image/png',
  'image/gif',
  'image/webp',
};

const Set<String> _supportedDocumentMimeTypes = {
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/msword',
  'text/plain',
  'text/csv',
  'text/tab-separated-values',
  'text/html',
  'application/rtf',
  'application/epub+zip',
};

enum AttachmentValidationError {
  tooManyFiles,
  imageTooLarge,
  documentTooLarge,
  unsupportedType,
  totalPayloadTooLarge,
}

class AttachmentValidationResult {
  final bool isValid;
  final AttachmentValidationError? error;
  final ChatAttachmentType? resolvedType;

  const AttachmentValidationResult._ok(this.resolvedType)
      : isValid = true,
        error = null;

  const AttachmentValidationResult._fail(this.error)
      : isValid = false,
        resolvedType = null;
}

class AttachmentValidator {
  const AttachmentValidator._();

  static AttachmentValidationResult validate({
    required String mimeType,
    required int fileSizeBytes,
    required List<ChatAttachment> existingAttachments,
  }) {
    if (existingAttachments.length >= _maxAttachmentsPerMessage) {
      return const AttachmentValidationResult._fail(
        AttachmentValidationError.tooManyFiles,
      );
    }

    final ChatAttachmentType type;
    if (_supportedImageMimeTypes.contains(mimeType)) {
      type = ChatAttachmentType.image;
    } else if (_supportedDocumentMimeTypes.contains(mimeType)) {
      type = ChatAttachmentType.document;
    } else {
      return const AttachmentValidationResult._fail(
        AttachmentValidationError.unsupportedType,
      );
    }

    if (type == ChatAttachmentType.image && fileSizeBytes > _maxImageSizeBytes) {
      return const AttachmentValidationResult._fail(
        AttachmentValidationError.imageTooLarge,
      );
    }
    if (type == ChatAttachmentType.document && fileSizeBytes > _maxDocumentSizeBytes) {
      return const AttachmentValidationResult._fail(
        AttachmentValidationError.documentTooLarge,
      );
    }

    final existingTotal = existingAttachments.fold(0, (sum, a) => sum + a.fileSizeBytes);
    if (existingTotal + fileSizeBytes > _maxTotalPayloadBytes) {
      return const AttachmentValidationResult._fail(
        AttachmentValidationError.totalPayloadTooLarge,
      );
    }

    return AttachmentValidationResult._ok(type);
  }

  static String errorMessage(AttachmentValidationError error) => switch (error) {
        AttachmentValidationError.tooManyFiles =>
          'Max 5 attachments per message',
        AttachmentValidationError.imageTooLarge =>
          'Image must be under 5 MB',
        AttachmentValidationError.documentTooLarge =>
          'Document must be under 10 MB',
        AttachmentValidationError.unsupportedType =>
          'Format not supported. Try JPEG, PNG, PDF, DOCX, or TXT',
        AttachmentValidationError.totalPayloadTooLarge =>
          'Total attachments too large — remove one and try again',
      };

  static ChatAttachmentType? resolveType(String mimeType) {
    if (_supportedImageMimeTypes.contains(mimeType)) return ChatAttachmentType.image;
    if (_supportedDocumentMimeTypes.contains(mimeType)) return ChatAttachmentType.document;
    return null;
  }

  static String? mimeTypeFromExtension(String? extension) {
    if (extension == null) return null;
    return _extToMime[extension.toLowerCase()];
  }

  static const _extToMime = <String, String>{
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'gif': 'image/gif',
    'webp': 'image/webp',
    'pdf': 'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'doc': 'application/msword',
    'txt': 'text/plain',
    'csv': 'text/csv',
    'tsv': 'text/tab-separated-values',
    'html': 'text/html',
    'htm': 'text/html',
    'rtf': 'application/rtf',
    'epub': 'application/epub+zip',
  };
}
