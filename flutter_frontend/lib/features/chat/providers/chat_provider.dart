// lib/features/chat/providers/chat_provider.dart
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:audio_session/audio_session.dart';
import 'package:uuid/uuid.dart';
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

  /// 来自后端 `topics.title_zh` / TaskPacket；空则界面用 `topicTitleUiLabel` 兜底。
  final String currentTopicTitleZh;
  final String currentRoleName;

  /// true while backend is generating a user-requested topic
  final bool isGeneratingTopic;

  // ── 主从分离架构新增：标识正在等待旁路的 JSON 辅导数据 ─────────────
  final bool isWaitingForTeachingData;

  ChatState({
    required this.status,
    required this.chatHistory,
    this.isFlipped = false,
    this.masteryProgress = 0.0,
    this.errorMessage,
    this.sessionReport,
    this.currentTopicTitle = "Simulation Practice",
    this.currentTopicTitleZh = '',
    this.currentRoleName = "AI Coach",
    this.isGeneratingTopic = false,
    this.isWaitingForTeachingData = false,
  });

  ChatState copyWith({
    ChatStatus? status,
    List<ChatTurn>? chatHistory,
    bool? isFlipped,
    double? masteryProgress,
    Object? errorMessage = _sentinel,
    Object? sessionReport = _sentinel,
    String? currentTopicTitle,
    String? currentTopicTitleZh,
    String? currentRoleName,
    bool? isGeneratingTopic,
    bool? isWaitingForTeachingData,
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
      currentTopicTitleZh: currentTopicTitleZh ?? this.currentTopicTitleZh,
      currentRoleName: currentRoleName ?? this.currentRoleName,
      isGeneratingTopic: isGeneratingTopic ?? this.isGeneratingTopic,
      isWaitingForTeachingData:
          isWaitingForTeachingData ?? this.isWaitingForTeachingData,
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

  /// 端到端延迟排查：从 stopListeningAndSubmit 到首包 PCM 的客户端阶段
  Stopwatch? _latencySw;
  bool _latencyLoggedFirstPcm = false;

  /// 与后端 [LATENCY]/[E2E] 对齐的轮次 id（随 user_finish_speaking 上报）
  String? _latencyTurnId;
  int? _latencyFirstPcmMs;

  // ── 高频文本流局部刷新：避免 ListView 全局重绘 ─────────────
  final ValueNotifier<String> activeUserTextNotifier = ValueNotifier<String>(
    '',
  );
  final ValueNotifier<String> activeAiTextNotifier = ValueNotifier<String>('');

  void _latencyLogClient(String stage, [String extra = '']) {
    final sw = _latencySw;
    if (sw == null) return;
    final ms = sw.elapsedMilliseconds;
    final tail = extra.isEmpty ? '' : ' $extra';
    final tid = _latencyTurnId;
    final turnSeg = (tid != null && tid.isNotEmpty) ? 'turn=$tid ' : '';
    debugPrint('[LATENCY][client] $turnSeg$stage +${ms}ms$tail');
  }

  @override
  ChatState build() {
    _recorder = AudioRecorder();
    _initAudioSessionAndPlayer();
    _initWebSocketListeners();
    Future.delayed(const Duration(milliseconds: 500), () => warmUpConnection());

    // ── 监听 WS 连接状态，自动处理重连后的握手重建 ─────────────────────────
    final wsClient = ref.read(websocketProvider);
    _connectionSubscription = wsClient.connectionStateStream.listen((
      connState,
    ) {
      debugPrint('[WS] connectionState → $connState  (status=${state.status})');
      if (connState == WsConnectionState.reconnecting) {
        _wasReconnecting = true;
        forceIdle(); // 重连期间强制空闲，防止录音/播放残留
      } else if (connState == WsConnectionState.connected && _wasReconnecting) {
        _wasReconnecting = false;
        Future.delayed(
          const Duration(milliseconds: 300),
          () => warmUpConnection(),
        );
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
      activeUserTextNotifier.dispose();
      activeAiTextNotifier.dispose();
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
      // 等待服务端 warmup_success（可带 user_settings）先到达，再回写本地 LMS，减少竞态
      await Future<void>.delayed(const Duration(milliseconds: 200));
      final settings = ref.read(settingsProvider);
      wsClient.sendCommand("update_lms_settings", {
        "user_id": userId,
        "depth_preference": settings.depthPreference,
        "new_topic_appetite": settings.newTopicAppetite,
        "learner_level": settings.learnerLevel,
      });
    } catch (_) {}
  }

  /// 服务端 DB 中的 user.settings（握手时下发）。有字段才覆盖本地。
  Future<void> _applyUserSettingsFromServer(Object? raw) async {
    if (raw is! Map) return;
    final us = Map<String, dynamic>.from(raw);
    if (us.isEmpty) return;
    final sn = ref.read(settingsProvider.notifier);
    final prefs = await SharedPreferences.getInstance();
    final lv = us['learner_level'];
    if (lv is String && lv.trim().isNotEmpty) {
      sn.setLearnerLevel(lv.trim());
      await prefs.setString('learner_level', lv.trim());
    }
    final dp = us['depth_preference'];
    if (dp is num) {
      sn.setDepthPreference(dp.toDouble());
    }
    final ap = us['new_topic_appetite'];
    if (ap is num) {
      sn.setNewTopicAppetite(ap.toDouble());
    }
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
      debugPrint("音频会话配置异常: $e");
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
      if (data['event'] == 'warmup_success') {
        await _applyUserSettingsFromServer(data['user_settings']);
      } else if (data['event'] == 'tts_finished') {
        if (_isInterrupting) return; // 打断期间忽略来自后端的 tts_finished
        _handleAudioFinished();
      } else if (data['event'] == 'asr_partial') {
        // 捕获流式转写数据并展示
        activeUserTextNotifier.value = data['text'] ?? '';
      } else if (data['event'] == 'ai_text_stream') {
        // ── 核心新增：捕获主 LLM 的流式回复，实现打字机效果 ──
        if (!state.isWaitingForTeachingData) {
          state = state.copyWith(isWaitingForTeachingData: true);
        }
        activeAiTextNotifier.value += (data['text'] ?? '');
      } else if (data['event'] == 'teaching_data') {
        // ── 核心新增：捕获副 LLM 生成的分析结果，移除骨架屏并结算 ──
        final d = data['data'];
        if (d['user_text'] != null && d['ai_text'] != null) {
          final newTurn = ChatTurn(
            userText: d['user_text'],
            aiText: d['ai_text'],
            rawTeachingData: d,
          );
          state = state.copyWith(
            chatHistory: [...state.chatHistory, newTurn],
            isWaitingForTeachingData: false,
          );
          activeUserTextNotifier.value = '';
          activeAiTextNotifier.value = '';
        }
      } else if (data['event'] == 'role_swapped') {
        state = state.copyWith(isFlipped: data['is_flipped']);
      } else if (data['event'] == 'topic_mastery_reached') {
        final progress = (data['progress'] as num?)?.toDouble() ?? 0.0;
        state = state.copyWith(masteryProgress: progress);
      } else if (data['event'] == 'topic_generating') {
        state = state.copyWith(isGeneratingTopic: true);
      } else if (data['event'] == 'topic_changed') {
        final zhRaw = data['topic_title_zh'];
        final zh = zhRaw is String ? zhRaw.trim() : '';
        state = state.copyWith(
          isGeneratingTopic: false,
          currentTopicTitle:
              data['topic_title'] as String? ?? state.currentTopicTitle,
          currentTopicTitleZh: zh.isNotEmpty ? zh : '',
          currentRoleName:
              data['role_name'] as String? ?? state.currentRoleName,
          masteryProgress: 0.0,
        );
      } else if (data['event'] == 'session_report') {
        _handleSessionReport(data);
      } else if (data['event'] == 'error') {
        final code = data['code'] as String? ?? 'UNKNOWN';
        final needIdle =
            code == 'LLM_TIMEOUT' ||
            code == 'LLM_ERROR' ||
            code == 'TOPIC_GENERATION_FAILED';
        if (needIdle) await forceIdle();
        final isZh = ref.read(settingsProvider).isChinese;
        final zh = (data['message_zh'] as String?)?.trim();
        final en = (data['message'] as String?)?.trim();
        final message = (isZh && zh != null && zh.isNotEmpty)
            ? zh
            : (en ??
                  (isZh
                      ? '出错了，请稍后再试。'
                      : 'Something went wrong. Please try again.'));
        state = state.copyWith(errorMessage: message);
      }
    }, onError: (_) => forceIdle());

    _audioSubscription = wsClient.audioStream.listen((audioBytes) {
      // 打断期间或非播放状态时，丢弃网络中残留的 PCM 包
      if (_isInterrupting) return;
      if (state.status == ChatStatus.speaking && _player.isPlaying) {
        if (!_latencyLoggedFirstPcm) {
          _latencyLoggedFirstPcm = true;
          _latencyFirstPcmMs = _latencySw?.elapsedMilliseconds;
          _latencyLogClient('05_first_pcm_chunk', 'len=${audioBytes.length}');
        }
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
    _latencyLogClient('06_tts_finished_event');

    final tid = _latencyTurnId;
    final sw = _latencySw;
    if (tid != null && tid.isNotEmpty && sw != null) {
      ref.read(websocketProvider).sendCommand('client_latency_report', {
        'trace_id': tid,
        'submit_to_first_pcm_ms': _latencyFirstPcmMs,
        'submit_to_tts_finished_ms': sw.elapsedMilliseconds,
        'had_first_pcm': _latencyFirstPcmMs != null,
        'client_report_wall_ms': DateTime.now().millisecondsSinceEpoch,
      });
    }

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
    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;

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
            // Fix B3: raised threshold -25 → -35 dBFS; min frames 5 → 4（约 400ms 有声）再判「已开口」，
            // 略缩有效句长、更快进入静默计时。
            if (amp.current > -35.0) {
              _noiseFrames++;
              if (_noiseFrames > 3) {
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
    _latencyTurnId = const Uuid().v4().replaceAll('-', '');
    _latencyFirstPcmMs = null;
    _latencyLoggedFirstPcm = false;
    _latencySw = Stopwatch()..start();
    _latencyLogClient('01_stopListening_submit_start');
    try {
      await _recorder.stop();
      _latencyLogClient('02_recorder_stopped');
      _ampSubscription?.cancel();
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel(); // Fix B5

      // 切换状态，唤起骨架屏，并重置本轮的 AI 流文本
      state = state.copyWith(
        status: ChatStatus.speaking,
        isWaitingForTeachingData: true,
      );
      activeAiTextNotifier.value = '';

      await _player.startPlayerFromStream(
        codec: Codec.pcm16,
        numChannels: 1,
        sampleRate: 24000,
        interleaved: true,
        bufferSize: 8192,
      );
      _latencyLogClient('03_player_stream_ready');
      final userId = await UserManager.getOrCreateUuid();
      final wallMs = DateTime.now().millisecondsSinceEpoch;
      ref.read(websocketProvider).sendCommand("user_finish_speaking", {
        "user_id": userId,
        "trace_id": _latencyTurnId,
        "client_submit_wall_ms": wallMs,
      });
      _latencyLogClient('04_ws_user_finish_sent');
    } catch (_) {
      _latencySw = null;
      _latencyTurnId = null;
      _latencyFirstPcmMs = null;
      _latencyLoggedFirstPcm = false;
      forceIdle();
    }
  }

  Future<void> forceIdle() async {
    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;
    _latencyLoggedFirstPcm = false;
    _isAutoLooping = false;
    _isStartingListen =
        false; // Fix B4: release mutex if we force-idle mid-start
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

    state = state.copyWith(
      status: ChatStatus.idle,
      isWaitingForTeachingData: false,
    );
    activeUserTextNotifier.value = '';
    activeAiTextNotifier.value = '';
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

    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;
    _latencyLoggedFirstPcm = false;

    // 清理界面数据并重入录音
    state = state.copyWith(isWaitingForTeachingData: false);
    activeUserTextNotifier.value = '';
    activeAiTextNotifier.value = '';

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
    _reportShowing =
        false; // Fix B7: resume autoMode cycle after report dismissed
    state = state.copyWith(sessionReport: null);
  }

  Future<void> requestTopic(String description) async {
    if (description.trim().isEmpty) return;
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      wsClient.sendCommand("request_topic", {
        "description": description.trim(),
      });
    } catch (_) {}
  }

  // 🌟 修改：主动更新设置时也传 learnerLevel
  Future<void> updateLmsSettings({
    required double depthPreference,
    required double newTopicAppetite,
    required String learnerLevel,
  }) async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("update_lms_settings", {
        "user_id": userId,
        "depth_preference": depthPreference,
        "new_topic_appetite": newTopicAppetite,
        "learner_level": learnerLevel,
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
      final zh = report.topicTitleZh?.trim() ?? '';
      state = state.copyWith(
        sessionReport: report,
        currentTopicTitle: report.topicTitle,
        currentTopicTitleZh: zh.isNotEmpty ? zh : state.currentTopicTitleZh,
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
