/// Tests the paste-detection heuristic in [MessageInput].
///
/// The composer has no OS-level paste callback that catches every route (the
/// Gboard clipboard chip and dictation arrive as ordinary commits), so provenance
/// is inferred from a bulk-insert delta: a single change that grows the text by
/// >= 30 characters is a paste. These pin that boundary, incremental typing, and
/// the reset-after-send behaviour so a later hand-typed message isn't mislabeled.
library;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aura/data/models/chat_attachment.dart';
import 'package:aura/data/models/chat_message_model.dart';
import 'package:aura/presentation/widgets/message_input.dart';

void main() {
  late TextEditingController controller;
  ChatMessageInputMethod? captured;

  Future<void> pumpInput(WidgetTester tester) async {
    captured = null;
    controller = TextEditingController();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: MessageInput(
            controller: controller,
            allowAttachments: false,
            onSend: (String text, List<ChatAttachment> _,
                ChatMessageInputMethod inputMethod) {
              captured = inputMethod;
            },
          ),
        ),
      ),
    );
  }

  Future<void> tapSend(WidgetTester tester) async {
    await tester.tap(find.byIcon(Icons.arrow_upward_rounded));
    await tester.pump();
  }

  /// Simulate character-by-character typing: each set grows the text by one, so
  /// every delta is far below the bulk-insert threshold.
  Future<void> typeIncrementally(WidgetTester tester, String value) async {
    for (var i = 1; i <= value.length; i++) {
      controller.text = value.substring(0, i);
      await tester.pump();
    }
  }

  testWidgets('a single large insert is classified as pasted', (tester) async {
    await pumpInput(tester);
    controller.text = 'x' * 60; // one contiguous insert, over the threshold
    await tester.pump();
    await tapSend(tester);
    expect(captured, ChatMessageInputMethod.pasted);
  });

  testWidgets('incremental typing of a long message stays typed', (tester) async {
    await pumpInput(tester);
    await typeIncrementally(tester, 'x' * 60); // 60 one-char inserts
    await tapSend(tester);
    expect(captured, ChatMessageInputMethod.typed);
  });

  testWidgets('a short one-shot insert stays typed (below threshold)',
      (tester) async {
    await pumpInput(tester);
    controller.text = 'hello there'; // 11 chars, under 30
    await tester.pump();
    await tapSend(tester);
    expect(captured, ChatMessageInputMethod.typed);
  });

  testWidgets('paste taint resets after send so the next typed message is typed',
      (tester) async {
    await pumpInput(tester);

    controller.text = 'x' * 60;
    await tester.pump();
    await tapSend(tester);
    expect(captured, ChatMessageInputMethod.pasted);

    // Send clears the field (resetting the flag); now type a short message.
    controller.text = 'hi';
    await tester.pump();
    await tapSend(tester);
    expect(captured, ChatMessageInputMethod.typed);
  });
}
