import 'dart:typed_data';

import 'package:file_picker/file_picker.dart';
import 'package:flutter_image_compress/flutter_image_compress.dart';
import 'package:image_picker/image_picker.dart';

import '../models/attachment_validator.dart';
import '../models/chat_attachment.dart';

class AttachmentProcessingResult {
  final ChatAttachment? attachment;
  final String? error;

  const AttachmentProcessingResult.success(ChatAttachment this.attachment) : error = null;
  const AttachmentProcessingResult.failure(String this.error) : attachment = null;
}

class AttachmentProcessor {
  final _imagePicker = ImagePicker();

  Future<XFile?> pickImageFromCamera() =>
      _imagePicker.pickImage(source: ImageSource.camera, imageQuality: 85);

  Future<List<XFile>> pickImagesFromGallery() =>
      _imagePicker.pickMultiImage(imageQuality: 85);

  Future<FilePickerResult?> pickFiles() => FilePicker.pickFiles(
        allowMultiple: true,
        withData: true,
        type: FileType.custom,
        // Kept in sync with attachment_validator.dart and backend/src/handlers/chat.py
        allowedExtensions: [
          'pdf', 'docx', 'doc', 'txt', 'csv', 'tsv', 'html', 'htm', 'rtf', 'epub',
          'jpg', 'jpeg', 'png', 'gif', 'webp',
        ],
      );

  Future<List<AttachmentProcessingResult>> processPickedImages(
    List<XFile> xFiles,
    List<ChatAttachment> existingAttachments,
  ) async {
    final results = await Future.wait(
      xFiles.map((xFile) => _processImage(xFile, existingAttachments)),
    );
    return results;
  }

  Future<AttachmentProcessingResult> processPickedImage(
    XFile xFile,
    List<ChatAttachment> existingAttachments,
  ) => _processImage(xFile, existingAttachments);

  Future<AttachmentProcessingResult> processPlatformFile(
    PlatformFile platformFile,
    List<ChatAttachment> existingAttachments,
  ) async {
    final bytes = platformFile.bytes;
    if (bytes == null) {
      return AttachmentProcessingResult.failure(
        'Could not read "${platformFile.name}". Try again.',
      );
    }

    final ext = platformFile.extension?.toLowerCase();
    final mimeType = AttachmentValidator.mimeTypeFromExtension(ext);
    if (mimeType == null) {
      return const AttachmentProcessingResult.failure(
        'Format not supported. Try JPEG, PNG, PDF, DOCX, or TXT',
      );
    }

    final resolvedType = AttachmentValidator.resolveType(mimeType);

    if (resolvedType == ChatAttachmentType.image) {
      return _compressAndCreateImage(bytes, platformFile.name, existingAttachments);
    }

    final validation = AttachmentValidator.validate(
      mimeType: mimeType,
      fileSizeBytes: bytes.length,
      existingAttachments: existingAttachments,
    );
    if (!validation.isValid) {
      return AttachmentProcessingResult.failure(
        AttachmentValidator.errorMessage(validation.error!),
      );
    }

    return AttachmentProcessingResult.success(ChatAttachment(
      fileName: platformFile.name,
      mimeType: mimeType,
      fileSizeBytes: bytes.length,
      bytes: bytes,
      type: ChatAttachmentType.document,
    ));
  }

  Future<AttachmentProcessingResult> _processImage(
    XFile xFile,
    List<ChatAttachment> existingAttachments,
  ) async {
    try {
      final bytes = await xFile.readAsBytes();
      final ext = xFile.path.split('.').last.toLowerCase();
      final mimeType = AttachmentValidator.mimeTypeFromExtension(ext) ?? 'image/jpeg';

      final validation = AttachmentValidator.validate(
        mimeType: mimeType,
        fileSizeBytes: bytes.length,
        existingAttachments: existingAttachments,
      );
      if (!validation.isValid) {
        return AttachmentProcessingResult.failure(
          AttachmentValidator.errorMessage(validation.error!),
        );
      }

      return _compressAndCreateImage(bytes, xFile.name, existingAttachments);
    } catch (_) {
      return const AttachmentProcessingResult.failure(
        'Could not load image. Please try again.',
      );
    }
  }

  Future<AttachmentProcessingResult> _compressAndCreateImage(
    Uint8List bytes,
    String fileName,
    List<ChatAttachment> existingAttachments,
  ) async {
    final results = await Future.wait([
      _compressImage(bytes, maxDimension: 1600, quality: 85),
      _compressImage(bytes, maxDimension: 200, quality: 75),
    ]);

    final compressedBytes = results[0] ?? bytes;
    final thumbnailBytes = results[1] ?? bytes;

    final compressedValidation = AttachmentValidator.validate(
      mimeType: 'image/jpeg',
      fileSizeBytes: compressedBytes.length,
      existingAttachments: existingAttachments,
    );
    if (!compressedValidation.isValid) {
      return AttachmentProcessingResult.failure(
        AttachmentValidator.errorMessage(compressedValidation.error!),
      );
    }

    return AttachmentProcessingResult.success(ChatAttachment(
      fileName: fileName,
      mimeType: 'image/jpeg',
      fileSizeBytes: compressedBytes.length,
      bytes: compressedBytes,
      type: ChatAttachmentType.image,
      thumbnail: thumbnailBytes,
    ));
  }

  Future<Uint8List?> _compressImage(
    Uint8List bytes, {
    required int maxDimension,
    required int quality,
  }) async {
    try {
      return await FlutterImageCompress.compressWithList(
        bytes,
        minWidth: maxDimension,
        minHeight: maxDimension,
        quality: quality,
        format: CompressFormat.jpeg,
        autoCorrectionAngle: true,
      );
    } catch (_) {
      return null;
    }
  }
}
