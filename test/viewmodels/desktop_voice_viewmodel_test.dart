import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/core/errors/app_exception.dart';
import 'package:aura/core/network/api_response.dart';
import 'package:aura/core/voice/voice_error_copy.dart';
import 'package:aura/data/models/voice_models.dart';
import 'package:aura/data/repositories/chat_repository.dart';
import 'package:aura/data/services/desktop/overlay_controller.dart';
import 'package:aura/data/services/voice_session_service.dart';
import 'package:aura/presentation/viewmodels/desktop_voice_viewmodel.dart';

import 'desktop_voice_viewmodel_test.mocks.dart';

@GenerateNiceMocks([
  MockSpec<VoiceSessionService>(),
  MockSpec<ChatRepository>(),
])
void main() {
  // Result is sealed; mockito cannot invent a dummy for unstubbed calls.
  provideDummy<Result<void>>(const Result.success(null));

  late MockVoiceSessionService voiceService;
  late MockChatRepository chatRepository;
  late OverlayController overlayController;
  late StreamController<VoiceServerEvent> voiceEvents;
  late StreamController<String?> authUserIds;

  setUp(() {
    voiceService = MockVoiceSessionService();
    chatRepository = MockChatRepository();
    overlayController = OverlayController();
    voiceEvents = StreamController<VoiceServerEvent>.broadcast();
    authUserIds = StreamController<String?>.broadcast();
    when(voiceService.events).thenAnswer((_) => voiceEvents.stream);
    when(voiceService.isConnected).thenReturn(false);
    when(voiceService.startSession(any))
        .thenAnswer((_) async => const Result.success(null));
    when(voiceService.close()).thenAnswer((_) async {});
    when(chatRepository.createSession(userId: anyNamed('userId')))
        .thenAnswer((_) async => 'session-1');
    when(chatRepository.saveMessage(any, userId: anyNamed('userId')))
        .thenAnswer((_) async => const Result.success(null));
  });

  tearDown(() async {
    await voiceEvents.close();
    await authUserIds.close();
  });

  DesktopVoiceViewModel buildViewModel({String? userId = 'uid-1'}) {
    return DesktopVoiceViewModel(
      voiceSessionService: voiceService,
      chatRepository: chatRepository,
      overlayController: overlayController,
      currentUserIdProvider: () => userId,
      authUserIdStream: authUserIds.stream,
    );
  }

  group('mic live on summon (decision 5)', () {
    test('summon from hidden starts a session and marks voice active',
        () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      verify(voiceService.startSession(any)).called(1);
      expect(overlayController.voiceActive, isTrue);
      expect(viewModel.status, VoiceSessionStatus.connecting);
    });

    test('summon while signed out never touches the voice service', () async {
      buildViewModel(userId: null);
      overlayController.summon();
      await pumpEventQueue();

      verifyNever(voiceService.startSession(any));
      expect(overlayController.voiceActive, isFalse);
    });

    test('pill -> panel restore does not start a second session', () async {
      buildViewModel();
      overlayController.summon();
      await pumpEventQueue();
      overlayController.setVoiceActive(true);
      overlayController.focusLost(); // panel -> pill
      overlayController.pillActivated(); // pill -> panel (not from hidden)
      await pumpEventQueue();

      verify(voiceService.startSession(any)).called(1);
    });
  });

  group('session lifecycle', () {
    test('esc ends the session through the controller wiring', () async {
      buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      overlayController.escPressed();
      await pumpEventQueue();

      verify(voiceService.close()).called(1);
    });

    test('session.ended resets state and releases the pill', () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();
      overlayController.focusLost(); // active voice -> pill

      voiceEvents.add(const VoiceServerEvent(type: 'session.ended'));
      await pumpEventQueue();

      expect(viewModel.status, VoiceSessionStatus.disconnected);
      expect(overlayController.voiceActive, isFalse);
      expect(overlayController.presentation, OverlayPresentation.hidden);
    });

    test('startSession failure surfaces the message and clears voice active',
        () async {
      when(voiceService.startSession(any)).thenAnswer(
          (_) async => Result.failure(AppException.unexpected('nope')));
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      expect(viewModel.status, VoiceSessionStatus.error);
      expect(viewModel.errorMessage, 'nope');
      expect(overlayController.voiceActive, isFalse);
    });

    test('sign-out ends a live session and resets to the signed-out state',
        () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();
      when(voiceService.isConnected).thenReturn(true);
      voiceEvents.add(const VoiceServerEvent(
          type: 'assistant.text.delta', text: 'Hey!'));
      await pumpEventQueue();

      authUserIds.add(null);
      await pumpEventQueue();

      verify(voiceService.close()).called(1);
      expect(viewModel.status, VoiceSessionStatus.disconnected);
      expect(viewModel.assistantCaption, isEmpty);
      expect(overlayController.voiceActive, isFalse);
    });

    test('auth emitting null while nothing is live is a no-op', () async {
      buildViewModel(userId: null);

      authUserIds.add(null);
      await pumpEventQueue();

      verifyNever(voiceService.close());
      expect(overlayController.voiceActive, isFalse);
    });
  });

  group('events -> state', () {
    test('captions flow from deltas and finals persist to drift', () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      voiceEvents
        ..add(const VoiceServerEvent(type: 'user.text.delta', text: 'hey bu'))
        ..add(const VoiceServerEvent(type: 'user.text.final', text: 'hey buddy'))
        ..add(const VoiceServerEvent(
            type: 'assistant.text.delta', text: 'Hey!'))
        ..add(const VoiceServerEvent(
            type: 'assistant.text.final', text: 'Hey! What are we doing?'));
      await pumpEventQueue();

      expect(viewModel.userCaption, 'hey buddy');
      expect(viewModel.assistantCaption, 'Hey! What are we doing?');
      verify(chatRepository.saveMessage(any, userId: anyNamed('userId')))
          .called(2);
    });

    test('session.error maps the code through the shared copy and drops the mic',
        () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      voiceEvents.add(const VoiceServerEvent(
        type: 'session.error',
        payload: {'code': 'agent_silent'},
      ));
      await pumpEventQueue();

      expect(viewModel.status, VoiceSessionStatus.error);
      expect(
        viewModel.errorMessage,
        voiceErrorMessageForCode(code: 'agent_silent', fallbackMessage: null),
      );
      expect(overlayController.voiceActive, isFalse);
    });

    test('mic settings hint appears only for the capture-failure copy',
        () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      voiceEvents.add(const VoiceServerEvent(
        type: 'session.error',
        payload: {'code': micCaptureFailedCode},
      ));
      await pumpEventQueue();

      expect(viewModel.showMicSettingsHint, isTrue);
    });

    test('session state events map to statuses', () async {
      final viewModel = buildViewModel();
      overlayController.summon();
      await pumpEventQueue();

      voiceEvents.add(const VoiceServerEvent(
          type: 'session.state', payload: {'state': 'speaking'}));
      await pumpEventQueue();
      expect(viewModel.status, VoiceSessionStatus.speaking);

      voiceEvents.add(const VoiceServerEvent(
          type: 'session.state', payload: {'state': 'listening'}));
      await pumpEventQueue();
      expect(viewModel.status, VoiceSessionStatus.listening);
    });
  });
}
