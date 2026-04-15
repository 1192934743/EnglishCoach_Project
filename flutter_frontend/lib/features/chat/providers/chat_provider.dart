import 'dart:async';
import 'dart:typed_data';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:audio_session/audio_session.dart';
import '../../../core/network/websocket_client.dart';
import '../../../core/providers/settings_provider.dart';
import '../../../core/network/user_manager.dart';

enum ChatStatus { idle, listening, speaking }

class ChatTurn {
  final String userText;
  final String aiText;
  final Map<String, dynamic> rawTeachingData;
  ChatTurn({
    required this.userText,
    required this.aiText,
    required this.rawTeachingData,
  });
}

class ChatState {
  final ChatStatus status;
  final List<ChatTurn> chatHistory;
  final bool isFlipped;

  ChatState({
    required this.status,
    required this.chatHistory,
    this.isFlipped = false,
  });

  ChatState copyWith({
    ChatStatus? status,
    List<ChatTurn>? chatHistory,
    bool? isFlipped,
  }) {
    return ChatState(
      status: status ?? this.status,
      chatHistory: chatHistory ?? this.chatHistory,
      isFlipped: isFlipped ?? this.isFlipped,
    );
  }
}

class ChatNotifier extends Notifier<ChatState> {
  late AudioRecorder _recorder;
  final FlutterSoundPlayer _player = FlutterSoundPlayer();

  StreamSubscription? _commandSubscription;
  StreamSubscription? _audioSubscription;
  StreamSubscription<Amplitude>? _ampSubscription;
  Timer? _silenceTimer;

  bool _hasSpoken = false;
  int _noiseFrames = 0;
  bool _isAutoLooping = false;
  int _totalBytesReceived = 0;
  DateTime? _playbackStartTime;

  @override
  ChatState build() {
    _recorder = AudioRecorder();
    _initAudioSessionAndPlayer();
    _initWebSocketListeners();
    Future.delayed(const Duration(milliseconds: 500), () => warmUpConnection());

    ref.onDispose(() {
      _commandSubscription?.cancel();
      _audioSubscription?.cancel();
      _ampSubscription?.cancel();
      _silenceTimer?.cancel();
      _recorder.dispose();
      _player.closePlayer();
    });
    return ChatState(
      status: ChatStatus.idle,
      chatHistory: [],
      isFlipped: false,
    );
  }

  Future<void> warmUpConnection() async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("ping", {"message": "warmup", "user_id": userId});
    } catch (_) {}
  }

  // 🌟 修复：发送切换角色指令时，必须带上 user_id
  Future<void> swapRole() async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("swap_role", {"user_id": userId});
    } catch (_) {}
  }

  // 🌟 修复：发送更新性格指令时，必须带上 user_id
  Future<void> updatePoliteness(int level) async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("update_politeness", {
        "level": level,
        "user_id": userId,
      });
    } catch (_) {}
  }

  Future<void> _initAudioSessionAndPlayer() async {
    try {
      final session = await AudioSession.instance;
      await session.configure(
        AudioSessionConfiguration(
          avAudioSessionCategory: AVAudioSessionCategory.playAndRecord,
          avAudioSessionCategoryOptions:
              AVAudioSessionCategoryOptions.allowBluetooth |
              AVAudioSessionCategoryOptions.defaultToSpeaker,
          avAudioSessionMode: AVAudioSessionMode.spokenAudio,
          avAudioSessionRouteSharingPolicy:
              AVAudioSessionRouteSharingPolicy.defaultPolicy,
          avAudioSessionSetActiveOptions: AVAudioSessionSetActiveOptions.none,
          androidAudioAttributes: const AndroidAudioAttributes(
            contentType: AndroidAudioContentType.speech,
            usage: AndroidAudioUsage.voiceCommunication,
          ),
          androidAudioFocusGainType: AndroidAudioFocusGainType.gain,
          androidWillPauseWhenDucked: true,
        ),
      );
      await _player.openPlayer();
    } catch (e) {
      print("音频会话配置异常: $e");
    }
  }

  Future<void> speakText(String text) async {
    await forceIdle();
    state = state.copyWith(status: ChatStatus.speaking);
    try {
      await _player.startPlayerFromStream(
        codec: Codec.pcm16,
        numChannels: 1,
        sampleRate: 24000,
        interleaved: true,
        bufferSize: 8192,
      );
      final userId = await UserManager.getOrCreateUuid();
      ref.read(websocketProvider).sendCommand("request_tts", {
        "text": text,
        "user_id": userId,
      });
    } catch (_) {
      forceIdle();
    }
  }

  void _initWebSocketListeners() {
    final wsClient = ref.read(websocketProvider);
    _commandSubscription = wsClient.commandStream.listen((data) {
      if (data['event'] == 'tts_finished') {
        _handleAudioFinished();
      } else if (data['event'] == 'teaching_data') {
        final d = data['data'];
        if (d['user_text'] != null && d['ai_text'] != null) {
          final newTurn = ChatTurn(
            userText: d['user_text'],
            aiText: d['ai_text'],
            rawTeachingData: d,
          );
          state = state.copyWith(chatHistory: [...state.chatHistory, newTurn]);
        }
      } else if (data['event'] == 'role_swapped') {
        state = state.copyWith(isFlipped: data['is_flipped']);
      }
    }, onError: (_) => forceIdle());

    _audioSubscription = wsClient.audioStream.listen((audioBytes) {
      if (state.status == ChatStatus.speaking && _player.isPlaying) {
        _playbackStartTime ??= DateTime.now();
        _totalBytesReceived += audioBytes.length;
        try {
          _player.uint8ListSink?.add(Uint8List.fromList(audioBytes));
        } catch (_) {}
      }
    });
  }

  Future<void> _handleAudioFinished() async {
    try {
      await _player.uint8ListSink?.close();
    } catch (_) {}
    if (_isAutoLooping) return;
    _isAutoLooping = true;

    if (_playbackStartTime != null && _totalBytesReceived > 0) {
      int durationMs = (_totalBytesReceived / 48.0).ceil();
      int elapsedMs = DateTime.now()
          .difference(_playbackStartTime!)
          .inMilliseconds;
      int timeLeftMs = durationMs - elapsedMs;
      if (timeLeftMs > 0)
        await Future.delayed(Duration(milliseconds: timeLeftMs));
    }
    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (_) {}
    _isAutoLooping = false;
    _playbackStartTime = null;
    _totalBytesReceived = 0;

    if (ref.read(settingsProvider).autoMode) {
      startListening();
    } else {
      state = state.copyWith(status: ChatStatus.idle);
    }
  }

  Future<void> startListening() async {
    if (state.status == ChatStatus.listening) return;
    if (_player.isPlaying) await _player.stopPlayer();
    final status = await Permission.microphone.request();
    if (!status.isGranted) return;

    try {
      final session = await AudioSession.instance;
      await session.setActive(true);
      final wsClient = ref.read(websocketProvider);
      await wsClient.connect();
      const config = RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 16000,
        numChannels: 1,
      );
      if (await _recorder.isRecording()) await _recorder.stop();
      final stream = await _recorder.startStream(config);

      state = state.copyWith(status: ChatStatus.listening);
      _hasSpoken = false;
      _noiseFrames = 0;

      stream.listen((data) {
        if (state.status == ChatStatus.listening) wsClient.sendAudio(data);
      });

      _ampSubscription?.cancel();
      _ampSubscription = _recorder
          .onAmplitudeChanged(const Duration(milliseconds: 100))
          .listen((amp) {
            if (state.status != ChatStatus.listening) return;
            final currentVadTimeout = ref.read(settingsProvider).vadTimeout;
            if (amp.current > -25.0) {
              _noiseFrames++;
              if (_noiseFrames > 2) {
                _hasSpoken = true;
                _silenceTimer?.cancel();
              }
            } else {
              _noiseFrames = 0;
              if (_hasSpoken) {
                if (_silenceTimer == null || !_silenceTimer!.isActive) {
                  _silenceTimer = Timer(
                    Duration(milliseconds: currentVadTimeout),
                    () => stopListeningAndSubmit(),
                  );
                }
              }
            }
          });
    } catch (e) {
      forceIdle();
    }
  }

  Future<void> stopListeningAndSubmit() async {
    if (state.status != ChatStatus.listening) return;
    try {
      await _recorder.stop();
      _ampSubscription?.cancel();
      _silenceTimer?.cancel();
      state = state.copyWith(status: ChatStatus.speaking);
      await _player.startPlayerFromStream(
        codec: Codec.pcm16,
        numChannels: 1,
        sampleRate: 24000,
        interleaved: true,
        bufferSize: 8192,
      );
      final userId = await UserManager.getOrCreateUuid();
      ref.read(websocketProvider).sendCommand("user_finish_speaking", {
        "user_id": userId,
      });
    } catch (_) {
      forceIdle();
    }
  }

  Future<void> forceIdle() async {
    _isAutoLooping = false;
    _playbackStartTime = null;
    _totalBytesReceived = 0;
    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (_) {}
    try {
      if (await _recorder.isRecording()) await _recorder.stop();
    } catch (_) {}
    _ampSubscription?.cancel();
    _silenceTimer?.cancel();
    state = state.copyWith(status: ChatStatus.idle);
  }

  Future<void> toggleButton() async {
    if (state.status == ChatStatus.idle) {
      await startListening();
    } else if (state.status == ChatStatus.listening) {
      await stopListeningAndSubmit();
    } else if (state.status == ChatStatus.speaking) {
      await forceIdle();
    }
  }
}

final chatProvider = NotifierProvider<ChatNotifier, ChatState>(
  () => ChatNotifier(),
);
