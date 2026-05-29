import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:aura/data/models/chat_attachment.dart';
import 'package:aura/data/models/chat_message_model.dart';

void main() {
  group('ChatMessageModel attachment serialization', () {
    test('toMap includes attachment_json when attachments present', () {
      final thumbnail = Uint8List.fromList([1, 2, 3]);
      final msg = ChatMessageModel(
        id: 'msg-1',
        text: 'Look at this',
        isUser: true,
        timestamp: DateTime(2026, 5, 24),
        channel: ChatMessageChannel.text,
        attachments: [
          ChatAttachment(
            id: 'att-1',
            fileName: 'photo.jpg',
            mimeType: 'image/jpeg',
            fileSizeBytes: 1024,
            bytes: Uint8List(10),
            type: ChatAttachmentType.image,
            thumbnail: thumbnail,
          ),
        ],
      );

      final map = msg.toMap();
      expect(map.containsKey('attachment_json'), isTrue);

      final parsed = jsonDecode(map['attachment_json'] as String) as List;
      expect(parsed.length, 1);
      expect(parsed[0]['fileName'], 'photo.jpg');
      expect(parsed[0]['mimeType'], 'image/jpeg');
      expect(parsed[0]['type'], 'image');
      expect(parsed[0]['thumbnail'], base64Encode(thumbnail));
    });

    test('toMap omits attachment_json when no attachments', () {
      final msg = ChatMessageModel(
        id: 'msg-2',
        text: 'Hello',
        isUser: true,
        timestamp: DateTime(2026, 5, 24),
        channel: ChatMessageChannel.text,
      );
      final map = msg.toMap();
      expect(map.containsKey('attachment_json'), isFalse);
    });

    test('fromMap rehydrates attachments with thumbnails', () {
      final thumbnail = Uint8List.fromList([10, 20, 30]);
      final attachmentJson = jsonEncode([
        {
          'fileName': 'doc.pdf',
          'mimeType': 'application/pdf',
          'type': 'document',
        },
        {
          'fileName': 'img.jpg',
          'mimeType': 'image/jpeg',
          'type': 'image',
          'thumbnail': base64Encode(thumbnail),
        },
      ]);

      final map = {
        'id': 'msg-3',
        'text': 'Check these',
        'is_user': true,
        'timestamp': '2026-05-24T00:00:00.000Z',
        'channel': 'text',
        'status': 'sent',
        'attachment_json': attachmentJson,
      };

      final msg = ChatMessageModel.fromMap(map);
      expect(msg.attachments, isNotNull);
      expect(msg.attachments!.length, 2);
      expect(msg.attachments![0].fileName, 'doc.pdf');
      expect(msg.attachments![0].type, ChatAttachmentType.document);
      expect(msg.attachments![0].thumbnail, isNull);
      expect(msg.attachments![1].fileName, 'img.jpg');
      expect(msg.attachments![1].type, ChatAttachmentType.image);
      expect(msg.attachments![1].thumbnail, equals(thumbnail));
    });

    test('fromMap returns null attachments for null attachment_json', () {
      final map = {
        'id': 'msg-4',
        'text': 'Plain',
        'is_user': true,
        'timestamp': '2026-05-24T00:00:00.000Z',
        'channel': 'text',
        'status': 'sent',
        'attachment_json': null,
      };
      final msg = ChatMessageModel.fromMap(map);
      expect(msg.attachments, isNull);
    });

    test('roundtrip preserves attachment metadata and thumbnails', () {
      final thumbnail = Uint8List.fromList([42, 43, 44]);
      final original = ChatMessageModel(
        id: 'msg-5',
        text: 'Roundtrip',
        isUser: true,
        timestamp: DateTime.utc(2026, 5, 24),
        channel: ChatMessageChannel.text,
        attachments: [
          ChatAttachment(
            fileName: 'test.png',
            mimeType: 'image/png',
            fileSizeBytes: 500,
            bytes: Uint8List(500),
            type: ChatAttachmentType.image,
            thumbnail: thumbnail,
          ),
        ],
      );

      final map = original.toMap();
      final restored = ChatMessageModel.fromMap(map);

      expect(restored.attachments, isNotNull);
      expect(restored.attachments!.length, 1);
      expect(restored.attachments![0].fileName, 'test.png');
      expect(restored.attachments![0].mimeType, 'image/png');
      expect(restored.attachments![0].type, ChatAttachmentType.image);
      expect(restored.attachments![0].thumbnail, equals(thumbnail));
      // Full bytes are NOT preserved on roundtrip (only thumbnails)
      expect(restored.attachments![0].bytes.isEmpty, isTrue);
    });
  });

  group('ChatMessageModel.toHistoryTurn with attachments', () {
    test('without includeAttachments returns plain text', () {
      final msg = ChatMessageModel(
        id: 'h-1',
        text: 'Describe this',
        isUser: true,
        timestamp: DateTime(2026, 5, 24),
        channel: ChatMessageChannel.text,
        attachments: [
          ChatAttachment(
            fileName: 'photo.jpg',
            mimeType: 'image/jpeg',
            fileSizeBytes: 100,
            bytes: Uint8List.fromList([0xFF, 0xD8]),
            type: ChatAttachmentType.image,
          ),
        ],
      );

      final turn = msg.toHistoryTurn();
      expect(turn['role'], 'user');
      expect(turn['content'], isA<String>());
      expect(turn['content'], 'Describe this');
    });

    test('with includeAttachments returns content blocks', () {
      final imgBytes = Uint8List.fromList([0xFF, 0xD8, 0xFF, 0xE0]);
      final msg = ChatMessageModel(
        id: 'h-2',
        text: 'What is this?',
        isUser: true,
        timestamp: DateTime(2026, 5, 24),
        channel: ChatMessageChannel.text,
        attachments: [
          ChatAttachment(
            fileName: 'photo.jpg',
            mimeType: 'image/jpeg',
            fileSizeBytes: imgBytes.length,
            bytes: imgBytes,
            type: ChatAttachmentType.image,
          ),
        ],
      );

      final turn = msg.toHistoryTurn(includeAttachments: true);
      expect(turn['role'], 'user');
      final content = turn['content'] as List;
      expect(content.length, 2);
      expect(content[0]['type'], 'image');
      expect(content[0]['source']['type'], 'base64');
      expect(content[0]['source']['data'], base64Encode(imgBytes));
      expect(content[1]['type'], 'text');
      expect(content[1]['text'], 'What is this?');
    });

    test('assistant messages never include attachment blocks', () {
      final msg = ChatMessageModel(
        id: 'h-3',
        text: 'I see a cat',
        isUser: false,
        timestamp: DateTime(2026, 5, 24),
        channel: ChatMessageChannel.text,
      );

      final turn = msg.toHistoryTurn(includeAttachments: true);
      expect(turn['content'], isA<String>());
    });

    test('empty bytes attachments treated as text-only', () {
      final msg = ChatMessageModel(
        id: 'h-4',
        text: 'Rehydrated message',
        isUser: true,
        timestamp: DateTime(2026, 5, 24),
        channel: ChatMessageChannel.text,
        attachments: [
          ChatAttachment(
            fileName: 'old.jpg',
            mimeType: 'image/jpeg',
            fileSizeBytes: 0,
            bytes: Uint8List(0),
            type: ChatAttachmentType.image,
          ),
        ],
      );

      final turn = msg.toHistoryTurn(includeAttachments: true);
      expect(turn['content'], isA<String>());
    });
  });
}
