// lib/features/chat/providers/chat_provider.dart
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:audio_session/audio_session.dart';
import '../../../core/network/websocket_client.dart';
import '../../../core/providers/settings_provider.dart';
import '../../../core/network/user_manager.dart';
import '../models/session_report_model.dart';

export '../models/session_report_model.dart';

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
  final double masteryProgress;
  final String? errorMessage;
  // ── LMS 新增字段 ───────────────────────────────────────────────────────
  /// 非 null 时触发报告卡弹出；dismiss 后调用 clearSessionReport() 置 null
  final SessionReport? sessionReport;
  final String currentTopicTitle;
  final String currentRoleName;
  /// true while backend is generating a user-requested topic
  final bool isGeneratingTopic;

  ChatState({
    required this.status,
    required this.chatHistory,
    this.isFlipped = false,
    this.masteryProgress = 0.0,
    this.errorMessage,
    this.sessionReport,
    this.currentTopicTitle = "Simulation Practice",
    this.currentRoleName = "AI Coach",
    this.isGeneratingTopic = false,
  });

  ChatState copyWith({
    ChatStatus? status,
    List<ChatTurn>? chatHistory,
    bool? isFlipped,
    double? masteryProgress,
    Object? errorMessage = _sentinel,
    Object? sessionReport = _sentinel,
    String? currentTopicTitle,
    String? currentRoleName,
    bool? isGeneratingTopic,
  }) {
    return ChatState(
      status: status ?? this.status,
      chatHistory: chatHistory ?? this.chatHistory,
      isFlipped: isFlipped ?? this.isFlipped,
      masteryProgress: masteryProgress ?? this.masteryProgress,
      errorMessage: errorMessage == _sentinel
          ? this.errorMessage
          : errorMessage as String?,
      sessionReport: sessionReport == _sentinel
          ? this.sessionReport
          : sessionReport as SessionReport?,
      currentTopicTitle: currentTopicTitle ?? this.currentTopicTitle,
      currentRoleName: currentRoleName ?? this.currentRoleName,
      isGeneratingTopic: isGeneratingTopic ?? this.isGeneratingTopic,
    );
  }
}

// copyWith 的哨兵值，用于区分「未传参」与「显式传 null」
const Object _sentinel = Object();

class ChatNotifier extends Notifier<ChatState> {
  late AudioRecorder _recorder;
  final FlutterSoundPlayer _player = FlutterSoundPlayer();

  StreamSubscription? _commandSubscription;
  StreamSubscription? _audioSubscription;
  StreamSubscription<Amplitude>? _ampSubscription;
  StreamSubscription? _connectionSubscription;
  Timer? _silenceTimer;

  bool _hasSpoken = false;
  int _noiseFrames = 0;
  bool _isAutoLooping = false;
  int _totalBytesReceived = 0;
  DateTime? _playbackStartTime;

  /// 打断标志：true 期间，所有 TTS 回调（音频流 / tts_finished）均被忽略
  bool _isInterrupting = false;
  /// 重连标志：上一次状态为 reconnecting，用于判断是否需要重新握手
  bool _wasReconnecting = false;
  /// Fix B4: 防止 startListening() 并发调用的互斥标志
  bool _isStartingListen = false;
  /// Fix B5: 最大录音计时器（静默超时保护）
  Timer? _maxListenTimer;
  /// Fix B7: 报告卡展示期间暂停 autoMode 自动开始录音
  bool _reportShowing = false;

  @override
  ChatState build() {
    _recorder = AudioRecorder();
    _initAudioSessionAndPlayer();
    _initWebSocketListeners();
    Future.delayed(const Duration(milliseconds: 500), () => warmUpConnection());

    // ── 监听 WS 连接状态，自动处理重连后的握手重建 ─────────────────────────
    final wsClient = ref.read(websocketProvider);
    _connectionSubscription = wsClient.connectionStateStream.listen((connState) {
      debugPrint('[WS] connectionState → $connState  (status=${state.status})');
      if (connState == WsConnectionState.reconnecting) {
        _wasReconnecting = true;
        forceIdle(); // 重连期间强制空闲，防止录音/播放残留
      } else if (connState == WsConnectionState.connected && _wasReconnecting) {
        _wasReconnecting = false;
        Future.delayed(const Duration(milliseconds: 300), () => warmUpConnection());
      }
    });

    ref.onDispose(() {
      _commandSubscription?.cancel();
      _audioSubscription?.cancel();
      _ampSubscription?.cancel();
      _connectionSubscription?.cancel();
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel();
      _recorder.dispose();
      _player.closePlayer();
    });
    return ChatState(
      status: ChatStatus.idle,
      chatHistory: [],
      isFlipped: false,
      masteryProgress: 0.0,
    );
  }

  Future<void> warmUpConnection() async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("ping", {"message": "warmup", "user_id": userId});
      // Sync LMS settings on every connection so backend always reflects current preferences
      final settings = ref.read(settingsProvider);
      wsClient.sendCommand("update_lms_settings", {
        "user_id": userId,
        "depth_preference": settings.depthPreference,
        "new_topic_appetite": settings.newTopicAppetite,
      });
    } catch (_) {}
  }

  Future<void> swapRole() async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("swap_role", {"user_id": userId});
    } catch (_) {}
  }

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
    // async 回调：允许在 error 分支 await forceIdle() 后再写 errorMessage
    _commandSubscription = wsClient.commandStream.listen((data) async {
      if (data['event'] == 'tts_finished') {
        if (_isInterrupting) return; // 打断期间忽略来自后端的 tts_finished
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
      } else if (data['event'] == 'topic_mastery_reached') {
        final progress = (data['progress'] as num?)?.toDouble() ?? 0.0;
        state = state.copyWith(masteryProgress: progress);
      } else if (data['event'] == 'topic_generating') {
        state = state.copyWith(isGeneratingTopic: true);
      } else if (data['event'] == 'topic_changed') {
        state = state.copyWith(
          isGeneratingTopic: false,
          currentTopicTitle: data['topic_title'] as String? ?? state.currentTopicTitle,
          currentRoleName: data['role_name'] as String? ?? state.currentRoleName,
          masteryProgress: 0.0,
        );
      } else if (data['event'] == 'session_report') {
        _handleSessionReport(data);
      } else if (data['event'] == 'error') {
        // LLM 超时 / 系统错误：先解锁 UI，再写 errorMessage 触发 SnackBar
        final code = data['code'] as String? ?? 'UNKNOWN';
        if (code == 'LLM_TIMEOUT' || code == 'LLM_ERROR') {
          await forceIdle(); // 确保 status=idle 后再推 errorMessage
          final message = data['message'] as String? ??
              'Something went wrong. Please try again.';
          state = state.copyWith(errorMessage: message);
        }
      }
    }, onError: (_) => forceIdle());

    _audioSubscription = wsClient.audioStream.listen((audioBytes) {
      // 打断期间或非播放状态时，丢弃网络中残留的 PCM 包
      if (_isInterrupting) return;
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
      if (timeLeftMs > 0) {
        await Future.delayed(Duration(milliseconds: timeLeftMs));
      }
    }
    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (_) {}
    _isAutoLooping = false;
    _playbackStartTime = null;
    _totalBytesReceived = 0;

    // Fix B7: do not auto-start while the session report card is visible
    if (ref.read(settingsProvider).autoMode && !_reportShowing) {
      startListening();
    } else {
      state = state.copyWith(status: ChatStatus.idle);
    }
  }

  Future<void> startListening() async {
    // Fix B4: mutex guard — prevents concurrent calls from racing past the status check
    if (_isStartingListen) return;
    if (state.status == ChatStatus.listening) return;
    _isStartingListen = true;

    try {
      if (_player.isPlaying) await _player.stopPlayer();
      final permStatus = await Permission.microphone.request();
      if (!permStatus.isGranted) return;

      // Fix B2: removed session.setActive(true) — the `record` package manages
      // Android AudioFocus internally; calling setActive() here creates a double
      // focus request that triggers onAudioFocusChange(-1) and breaks recording.
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

      // Fix B5: max-listen safety timer — if no voice is detected within 30s,
      // forceIdle() to prevent the UI from being permanently stuck in "Listening".
      _maxListenTimer?.cancel();
      _maxListenTimer = Timer(const Duration(seconds: 30), () {
        if (state.status == ChatStatus.listening) {
          // If the user started speaking but silence timer never fired, submit anyway.
          if (_hasSpoken) {
            stopListeningAndSubmit();
          } else {
            forceIdle();
          }
        }
      });

      _ampSubscription?.cancel();
      _ampSubscription = _recorder
          .onAmplitudeChanged(const Duration(milliseconds: 100))
          .listen((amp) {
            if (state.status != ChatStatus.listening) return;
            final currentVadTimeout = ref.read(settingsProvider).vadTimeout;
            // Fix B3: raised threshold -25 → -35 dBFS, and min frames 3 → 5 (500ms).
            // Android's AGC easily pushes ambient noise above -25 dBFS causing false
            // "has spoken" detections within 300ms of starting.
            if (amp.current > -35.0) {
              _noiseFrames++;
              if (_noiseFrames > 4) {
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
    } catch (e, st) {
      // Print the real exception so we can see exactly what failed
      debugPrint('[startListening] EXCEPTION: $e');
      debugPrint('[startListening] STACKTRACE: $st');
      forceIdle();
    } finally {
      // Fix B4: always release the mutex so future calls are not permanently blocked
      _isStartingListen = false;
    }
  }

  Future<void> stopListeningAndSubmit() async {
    if (state.status != ChatStatus.listening) return;
    try {
      await _recorder.stop();
      _ampSubscription?.cancel();
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel(); // Fix B5
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
    _isStartingListen = false; // Fix B4: release mutex if we force-idle mid-start
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
    _maxListenTimer?.cancel(); // Fix B5: cancel the safety timer
    state = state.copyWith(status: ChatStatus.idle);
  }

  /// P0 打断机制：AI 说话途中用户开口 → 立刻停播 + 清空 PCM + 通知后端 + 开始录音
  Future<void> interruptAndListen() async {
    if (state.status != ChatStatus.speaking) return;

    // ── 1. 设置打断标志，屏蔽所有后续 TTS 回调 ───────────────────────────
    _isInterrupting = true;
    _isAutoLooping = false;

    // ── 2. 关闭 PCM 输入 sink（清空 flutter_sound 内部缓冲区）─────────────
    try {
      await _player.uint8ListSink?.close();
    } catch (_) {}

    // ── 3. 强制停止播放器（立刻停音）─────────────────────────────────────
    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (_) {}

    // ── 4. 清零前端 PCM 追踪状态 ──────────────────────────────────────────
    _totalBytesReceived = 0;
    _playbackStartTime = null;

    // ── 5. 通知后端取消 TTS，同时清空后端 audio_buffer ────────────────────
    ref.read(websocketProvider).sendCommand("cancel_tts", {});

    // ── 6. 解除打断标志，切换到录音状态 ──────────────────────────────────
    _isInterrupting = false;
    await startListening();
  }

  /// UI 展示错误 Snackbar 后调用，清空 errorMessage 防止重复弹出
  void clearError() {
    state = state.copyWith(errorMessage: null);
  }

  /// 报告卡 dismiss 后调用，防止重复弹出，并恢复 autoMode 录音循环
  void clearSessionReport() {
    _reportShowing = false; // Fix B7: resume autoMode cycle after report dismissed
    state = state.copyWith(sessionReport: null);
  }

  Future<void> requestTopic(String description) async {
    if (description.trim().isEmpty) return;
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      wsClient.sendCommand("request_topic", {"description": description.trim()});
    } catch (_) {}
  }

  Future<void> updateLmsSettings({
    required double depthPreference,
    required double newTopicAppetite,
  }) async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("update_lms_settings", {
        "user_id": userId,
        "depth_preference": depthPreference,
        "new_topic_appetite": newTopicAppetite,
      });
    } catch (_) {}
  }

  void _handleSessionReport(Map<String, dynamic> data) {
    final stage = data['stage'] as String? ?? 'preliminary';

    if (stage == 'preliminary') {
      // Fix B7: pause autoMode while report card is visible, and stop any
      // ongoing recording/playback so the UI is clean when the sheet appears.
      _reportShowing = true;
      forceIdle(); // async, but fire-and-forget is fine here

      final report = SessionReport.fromJson(data);
      state = state.copyWith(
        sessionReport: report,
        currentTopicTitle: report.topicTitle,
        currentRoleName: report.topicTitle,
      );
    } else if (stage == 'final') {
      final existing = state.sessionReport;
      final incomingId = data['session_id'] as String? ?? '';
      if (existing != null && existing.sessionId == incomingId) {
        state = state.copyWith(sessionReport: existing.mergeWithFinal(data));
      }
    }
  }

  Future<void> toggleButton() async {
    if (state.status == ChatStatus.idle) {
      await startListening();
    } else if (state.status == ChatStatus.listening) {
      await stopListeningAndSubmit();
    } else if (state.status == ChatStatus.speaking) {
      // 🛑 打断：不再只是停到 idle，而是立刻开始新一轮录音
      await interruptAndListen();
    }
  }
}

final chatProvider = NotifierProvider<ChatNotifier, ChatState>(
  () => ChatNotifier(),
);
