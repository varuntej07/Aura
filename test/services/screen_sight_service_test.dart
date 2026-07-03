import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:mockito/annotations.dart';
import 'package:mockito/mockito.dart';

import 'package:aura/data/models/voice_models.dart';
import 'package:aura/data/services/desktop/desktop_screen_capture_service.dart';
import 'package:aura/data/services/desktop/pointing_overlay_service.dart';
import 'package:aura/data/services/desktop/screen_sight_service.dart';
import 'package:aura/data/services/voice_session_service.dart';

import 'screen_sight_service_test.mocks.dart';

/// Pins the push-to-look contract (product decision: screen sight is armed by
/// the user per session, never ambient):
///   - nothing EVER captures while unarmed;
///   - arming captures immediately on a live session and once per spoken turn
///     (first user.text.delta), not on every delta;
///   - arming ends with the session (ended / error), never persists;
///   - frames carry the geometry attributes the pointing math needs.
@GenerateNiceMocks([
  MockSpec<VoiceSessionService>(),
  MockSpec<DesktopScreenCaptureService>(),
  MockSpec<PointingOverlayService>(),
])
void main() {
  late MockVoiceSessionService voiceService;
  late MockDesktopScreenCaptureService captureService;
  late MockPointingOverlayService pointingService;
  late StreamController<VoiceServerEvent> voiceEvents;
  late ScreenSightService screenSight;

  final frame = CapturedScreenFrame(
    jpegBytes: Uint8List.fromList([1, 2, 3]),
    monitorLeftPx: 0,
    monitorTopPx: 0,
    monitorWidthPx: 2560,
    monitorHeightPx: 1440,
    scaleFactor: 1.5,
    jpegWidthPx: 1280,
    jpegHeightPx: 720,
  );

  setUp(() {
    voiceService = MockVoiceSessionService();
    captureService = MockDesktopScreenCaptureService();
    pointingService = MockPointingOverlayService();
    voiceEvents = StreamController<VoiceServerEvent>.broadcast();
    when(voiceService.events).thenAnswer((_) => voiceEvents.stream);
    when(voiceService.isConnected).thenReturn(true);
    when(voiceService.sendScreenFrame(any, attributes: anyNamed('attributes')))
        .thenAnswer((_) async {});
    when(captureService.captureCursorDisplay()).thenAnswer((_) async => frame);
    when(pointingService.pointAt(
      geometry: anyNamed('geometry'),
      jpegX: anyNamed('jpegX'),
      jpegY: anyNamed('jpegY'),
      label: anyNamed('label'),
    )).thenAnswer((_) async {});

    screenSight = ScreenSightService()
      ..attach(
        voiceService: voiceService,
        captureService: captureService,
        pointingService: pointingService,
      );
  });

  tearDown(() async {
    await voiceEvents.close();
  });

  Future<void> emit(String type) async {
    voiceEvents.add(VoiceServerEvent(type: type));
    await pumpEventQueue();
  }

  test('unarmed session never captures, whatever events flow', () async {
    await emit('session.ready');
    await emit('user.text.delta');
    await emit('user.text.final');

    verifyNever(captureService.captureCursorDisplay());
    verifyNever(
        voiceService.sendScreenFrame(any, attributes: anyNamed('attributes')));
    expect(screenSight.armed, isFalse);
  });

  test('arming on a live session captures and sends immediately', () async {
    screenSight.toggleArmed();
    await pumpEventQueue();

    expect(screenSight.armed, isTrue);
    verify(captureService.captureCursorDisplay()).called(1);
    final captured = verify(voiceService.sendScreenFrame(any,
            attributes: captureAnyNamed('attributes')))
        .captured
        .single as Map<String, String>;
    // The pointing math needs the full geometry chain on every frame.
    expect(captured['jpeg_width_px'], '1280');
    expect(captured['jpeg_height_px'], '720');
    expect(captured['monitor_width_px'], '2560');
    expect(captured['scale_factor'], '1.5');
    expect(captured['frame_id'], isNotEmpty);
  });

  test('captures once per turn: first delta only, resets on final', () async {
    screenSight.toggleArmed();
    await pumpEventQueue();
    clearInteractions(captureService);

    await emit('user.text.delta');
    await emit('user.text.delta'); // same turn: no second capture
    verify(captureService.captureCursorDisplay()).called(1);

    await emit('user.text.final');
    await emit('user.text.delta'); // next turn captures again
    verify(captureService.captureCursorDisplay()).called(1);
  });

  test('toggle again disarms and stops capturing', () async {
    screenSight.toggleArmed();
    await pumpEventQueue();
    screenSight.toggleArmed();
    clearInteractions(captureService);

    await emit('user.text.delta');
    expect(screenSight.armed, isFalse);
    verifyNever(captureService.captureCursorDisplay());
  });

  test('arming ends with the session: ended and error both disarm', () async {
    screenSight.toggleArmed();
    await pumpEventQueue();
    await emit('session.ended');
    expect(screenSight.armed, isFalse);

    screenSight.toggleArmed();
    await pumpEventQueue();
    await emit('session.error');
    expect(screenSight.armed, isFalse);
  });

  test('armed while disconnected: no capture until session.ready', () async {
    when(voiceService.isConnected).thenReturn(false);
    screenSight.toggleArmed();
    await pumpEventQueue();
    verifyNever(captureService.captureCursorDisplay());

    when(voiceService.isConnected).thenReturn(true);
    await emit('session.ready');
    verify(captureService.captureCursorDisplay()).called(1);
  });

  test('a failed capture degrades silently: nothing is sent', () async {
    when(captureService.captureCursorDisplay()).thenAnswer((_) async => null);
    screenSight.toggleArmed();
    await pumpEventQueue();

    verifyNever(
        voiceService.sendScreenFrame(any, attributes: anyNamed('attributes')));
    expect(screenSight.armed, isTrue); // arm state survives a flaky capture
  });

  test('element.point routes to the pointing service with the frame geometry',
      () async {
    screenSight.toggleArmed(); // sends frame f1, geometry retained
    await pumpEventQueue();

    voiceEvents.add(VoiceServerEvent(type: 'element.point', payload: {
      'x': 640,
      'y': 360,
      'label': 'save button',
      'frame_id': 'f1',
    }));
    await pumpEventQueue();

    final geometry = verify(pointingService.pointAt(
      geometry: captureAnyNamed('geometry'),
      jpegX: 640,
      jpegY: 360,
      label: 'save button',
    )).captured.single as ScreenFrameGeometry;
    expect(geometry.monitorWidthPx, 2560);
    expect(geometry.scaleFactor, 1.5);
  });

  test('element.point with no sent frame is loudly skipped, never animated',
      () async {
    voiceEvents.add(VoiceServerEvent(type: 'element.point', payload: {
      'x': 10,
      'y': 20,
      'label': 'ghost',
      'frame_id': 'f9',
    }));
    await pumpEventQueue();

    verifyNever(pointingService.pointAt(
      geometry: anyNamed('geometry'),
      jpegX: anyNamed('jpegX'),
      jpegY: anyNamed('jpegY'),
      label: anyNamed('label'),
    ));
  });
}
