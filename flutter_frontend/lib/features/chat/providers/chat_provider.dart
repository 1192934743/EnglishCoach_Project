// lib/features/chat/providers/chat_provider.dart
//
// 职责：
// 1. 定义 ChatState / ChatStatus / ChatTurn（已保留）
// 2. 管理 UI 状态（会话历史、进度、角色翻转等）
// 3. 响应用户 action（toggleButton、swapRole 等）
// 4. 委托音频控制 → AudioController
// 5. 委托 WS 消息解析 → ws_message_parser.dart
//
// 不再包含：音频录制/播放逻辑、WS 消息解析（已拆分到 services/）

import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:uuid/uuid.dart';

import '../../../core/network/websocket_client.dart';
import '../../../core/providers/settings_provider.dart';
import '../../../core/network/user_manager.dart';
import '../models/session_report_model.dart';
import '../models/ws_message_models.dart';
import '../services/audio_controller.dart';
import '../services/ws_message_parser.dart';

export '../models/session_report_model.dart';
export '../models/ws_message_models.dart' show
    WsSessionReportPreliminary,
    WsSessionReportFinal,
    UserSettings,
    WsEventType,
    WsWarmupSuccess,
    WsTtsFinished,
    WsTeachingData,
    WsRoleSwapped,
    WsTopicMastery,
    WsTopicGenerating,
    WsTopicChanged,
    WsSessionReport,
    WsError,
    WsAsrPartial,
    WsTestAiReply;

// ── 类型别名 ───────────────────────────────────────────────────────────────

enum ChatStatus { idle, listening, speaking }

// ── Debug ──────────────────────────────────────────────────────────────
// true:  依赖 flutter_sound 的 whenFinished 回调切到用户说话（ttsFinished 只关流不等待）。
//         测试方法：手动把这里改为 true，真机跑一遍看音频尾巴是否完整、切换是否流畅。
// false: 使用 ttsFinished 事件 + 300ms 缓冲后关流（现有机制）。
const bool _kUseNativeAudioCallback = kDebugMode;

// ── 数据模型 ─────────────────────────────────────────────────────────────

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
  final SessionReport? sessionReport;
  final String currentTopicTitle;
  final String currentTopicTitleZh;
  final String currentRoleName;
  final bool isGeneratingTopic;

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
    );
  }
}

const Object _sentinel = Object();

// ── Provider ──────────────────────────────────────────────────────────────

final chatProvider = NotifierProvider<ChatNotifier, ChatState>(
  () => ChatNotifier(),
);

// ── ChatNotifier ────────────────────────────────────────────────────────

class ChatNotifier extends Notifier<ChatState> {
  // ── 音频控制器（委托）─────────────────────────────────────────────
  late final AudioController _audio;

  // ── 订阅管理 ──────────────────────────────────────────────────────
  StreamSubscription? _commandSubscription;
  StreamSubscription? _audioSubscription;
  StreamSubscription? _connectionSubscription;
  StreamSubscription? _pcmSubscription;

  // ── VAD 状态 ──────────────────────────────────────────────────────
  Timer? _silenceTimer;
  Timer? _maxListenTimer;
  bool _hasSpoken = false;
  int _noiseFrames = 0;

  // ── AutoMode / Interrupt ──────────────────────────────────────────
  bool _isInterrupting = false;
  bool _wasReconnecting = false;
  bool _isStartingListen = false;  // Fix B4
  bool _reportShowing = false;       // Fix B7

  // ── Latency 追踪 ───────────────────────────────────────────────────
  Stopwatch? _latencySw;
  bool _latencyLoggedFirstPcm = false;
  String? _latencyTurnId;
  int? _latencyFirstPcmMs;

  @override
  ChatState build() {
    _audio = AudioController();
    _initAudio();
    _initWebSocketListeners();
    Future.delayed(const Duration(milliseconds: 500), () => warmUpConnection());

    final wsClient = ref.read(websocketProvider);
    _connectionSubscription = wsClient.connectionStateStream.listen((connState) {
      debugPrint('[WS_conn] state=$connState, status=${state.status}');
      if (connState == WsConnectionState.reconnecting) {
        _wasReconnecting = true;
        debugPrint('[WS_conn] reconnecting detected, calling forceIdle()');
        forceIdle();
      } else if (connState == WsConnectionState.connected && _wasReconnecting) {
        _wasReconnecting = false;
        debugPrint('[WS_conn] reconnected, scheduling warmup');
        Future.delayed(const Duration(milliseconds: 300), () => warmUpConnection());
      }
    });

    ref.onDispose(() {
      _commandSubscription?.cancel();
      _audioSubscription?.cancel();
      _connectionSubscription?.cancel();
      _pcmSubscription?.cancel();
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel();
      _audio.dispose();
    });

    return ChatState(status: ChatStatus.idle, chatHistory: []);
  }

  // ── 初始化 ────────────────────────────────────────────────────────

  Future<void> _initAudio() async {
    await _audio.init();
  }

  void _initWebSocketListeners() {
    final wsClient = ref.read(websocketProvider);

    // WS 文本消息处理
    _commandSubscription = wsClient.commandStream.listen((data) async {
      _handleIncomingMessage(data);
    });

    // WS 音频流处理
    _audioSubscription = wsClient.audioStream.listen((audioBytes) {
      debugPrint('[audioStream] received bytes=${audioBytes.length}');
      if (_isInterrupting) return;
      if (state.status == ChatStatus.speaking && _audio.isPlaying) {
        if (!_latencyLoggedFirstPcm) {
          _latencyLoggedFirstPcm = true;
          _latencyFirstPcmMs = _latencySw?.elapsedMilliseconds;
          _latencyLogClient('05_first_pcm_chunk', 'len=${audioBytes.length}');
        }
        _audio.writePcm(Uint8List.fromList(audioBytes));
      } else {
        debugPrint('[audioStream] DROP bytes=${audioBytes.length} status=${state.status} isPlaying=${_audio.isPlaying}');
      }
    });
  }

  // ── WS 入站消息处理（委托给 WsMessageParser）────────────────────────

  void _handleIncomingMessage(Map<String, dynamic> data) {
    final event = parseEventType(data);
    if (event == null) return;

    switch (event) {
      case WsEventType.warmupSuccess:
        _handleWarmupSuccess(data);
        break;
      case WsEventType.ttsFinished:
        _handleTtsFinished(data);
        break;
      case WsEventType.teachingData:
        _handleTeachingData(data);
        break;
      case WsEventType.roleSwapped:
        _handleRoleSwapped(data);
        break;
      case WsEventType.topicMasteryReached:
        _handleTopicMastery(data);
        break;
      case WsEventType.topicGenerating:
        _handleTopicGenerating(data);
        break;
      case WsEventType.topicChanged:
        _handleTopicChanged(data);
        break;
      case WsEventType.sessionReport:
        _handleSessionReport(data);
        break;
      case WsEventType.error:
        _handleError(data);
        break;
      default:
        break;
    }
  }

  void _handleWarmupSuccess(Map<String, dynamic> data) {
    final parsed = parseWarmupSuccess(data);
    if (parsed?.userSettings != null) {
      _applyUserSettings(parsed!.userSettings!);
    }
  }

  void _handleTtsFinished(Map<String, dynamic> data) {
    if (_isInterrupting) return;
    debugPrint('[ttsFinished] ▶ received, status=${state.status}');
    try {
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

      // ttsFinished 到达时，服务端音频已全部发出。
      // 两种模式：
      //   _kUseNativeAudioCallback=true:  用进度监听器估算播放结束时刻，
      //     届时直接调用 _onAudioFinished。无需等 ttsFinished 触发关流。
      //   _kUseNativeAudioCallback=false: 等 300ms 缓冲后关流（ttsFinished 触发）。
      if (_kUseNativeAudioCallback) {
        final totalBytes = _audio.totalPcmBytes;
        // 24000Hz * 1ch * 2bytes = 48000 bytes/s
        final audioDurationMs = (totalBytes ~/ 48000 * 1000).clamp(500, 8000);
        // 留 200ms 缓冲，确保播放器处理完最后一块
        final fireAtMs = audioDurationMs + 200;
        debugPrint('[ttsFinished] nativeCallbackMode: totalBytes=$totalBytes → fire in ${fireAtMs}ms');
        Timer(Duration(milliseconds: fireAtMs), () {
          _onAudioFinished();
        });
      } else {
        const _streamFinalizeDelayMs = 300;
        debugPrint('[ttsFinished] ttsFinishedMode: closing stream in ${_streamFinalizeDelayMs}ms');
        Future.delayed(const Duration(milliseconds: _streamFinalizeDelayMs), () {
          _audio.closePcmStream();
        });
      }
    } catch (e, st) {
      debugPrint('[ttsFinished] EXCEPTION: $e STACKTRACE: $st');
      state = state.copyWith(status: ChatStatus.idle);
    }
  }

  void _handleTeachingData(Map<String, dynamic> data) {
    final parsed = parseTeachingData(data);
    if (parsed == null) return;
    if (parsed.userText.isEmpty && parsed.aiText.isEmpty) return;
    final newTurn = ChatTurn(
      userText: parsed.userText,
      aiText: parsed.aiText,
      rawTeachingData: Map<String, dynamic>.from(data['data'] ?? {}),
    );
    state = state.copyWith(chatHistory: [...state.chatHistory, newTurn]);
  }

  void _handleRoleSwapped(Map<String, dynamic> data) {
    final parsed = parseRoleSwapped(data);
    if (parsed != null) {
      state = state.copyWith(isFlipped: parsed.isFlipped);
    }
  }

  void _handleTopicMastery(Map<String, dynamic> data) {
    final parsed = parseTopicMastery(data);
    if (parsed != null) {
      state = state.copyWith(masteryProgress: parsed.progress);
    }
  }

  void _handleTopicGenerating(Map<String, dynamic> data) {
    state = state.copyWith(isGeneratingTopic: true);
  }

  void _handleTopicChanged(Map<String, dynamic> data) {
    final parsed = parseTopicChanged(data);
    if (parsed == null) return;
    state = state.copyWith(
      isGeneratingTopic: false,
      currentTopicTitle: parsed.topicTitle.isNotEmpty
          ? parsed.topicTitle
          : state.currentTopicTitle,
      currentTopicTitleZh: parsed.topicTitleZh ?? '',
      currentRoleName: parsed.roleName.isNotEmpty
          ? parsed.roleName
          : state.currentRoleName,
      masteryProgress: 0.0,
    );
  }

  void _handleSessionReport(Map<String, dynamic> data) {
    final parsed = parseSessionReport(data);
    if (parsed == null) return;

    if (parsed is WsSessionReportPreliminary) {
      _reportShowing = true;
      forceIdle();
      final report = SessionReport.fromJson(data);
      state = state.copyWith(
        sessionReport: report,
        currentTopicTitle: report.topicTitle,
        currentTopicTitleZh: report.topicTitleZh?.trim() ?? state.currentTopicTitleZh,
      );
    } else if (parsed is WsSessionReportFinal) {
      final existing = state.sessionReport;
      if (existing != null && existing.sessionId == parsed.sessionId) {
        state = state.copyWith(sessionReport: existing.mergeWithFinal(data));
      }
    }
  }

  void _handleError(Map<String, dynamic> data) {
    final parsed = parseError(data);
    if (parsed == null) return;
    final needIdle = parsed.isLlmTimeout || parsed.isLlmError || parsed.isTopicGenerationFailed;
    if (needIdle) forceIdle();
    final isZh = ref.read(settingsProvider).isChinese;
    final zh = parsed.messageZh?.trim();
    final en = parsed.message?.trim();
    final message = (isZh && zh != null && zh.isNotEmpty)
        ? zh
        : (en ?? (isZh ? '出错了，请稍后再试。' : 'Something went wrong. Please try again.'));
    state = state.copyWith(errorMessage: message);
  }

  // ── 音频回调 ──────────────────────────────────────────────────────

  void _onAudioFinished() {
    final totalBytes = _audio.totalPcmBytes;
    debugPrint('[onAudioFinished] ▶ called, _pcmSubscription=$_pcmSubscription, status=${state.status}, total_pcm_bytes=$totalBytes');
    // 幂等检查：如果已清理则直接返回
    if (_pcmSubscription == null) {
      debugPrint('[onAudioFinished] idempotency guard: _pcmSubscription already null, returning');
      return;
    }
    _pcmSubscription?.cancel();
    _pcmSubscription = null;
    debugPrint('[onAudioFinished] subscription cancelled');

    // closePcmStream() 在 ttsFinished 的延迟后被调用，
    // 这会触发 _pcmSubscription.onDone() → 本函数被调用。
    // 此处无需再关闭 stream。

    // stopPlayer() 在 microtask 里执行，此时播放器已经没有数据来源。
    // 延迟一帧让播放器有机会完成最后几帧的播放。
    Future.microtask(() async {
      try {
        await _audio.stopPlayer();
        debugPrint('[onAudioFinished] player stopped');
      } catch (e) {
        debugPrint('[onAudioFinished] stopPlayer exception: $e');
      }
    });

    _audio.resetPlaybackState();
    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;

    final auto = ref.read(settingsProvider).autoMode;
    debugPrint('[onAudioFinished] autoMode=$auto, _reportShowing=$_reportShowing');
    if (auto && !_reportShowing) {
      debugPrint('[onAudioFinished] calling startListening()...');
      startListening();
    } else {
      debugPrint('[onAudioFinished] setting status=idle');
      state = state.copyWith(status: ChatStatus.idle);
    }
  }

  // ── 用户操作 ──────────────────────────────────────────────────────

  Future<void> warmUpConnection() async {
    final wsClient = ref.read(websocketProvider);
    try {
      await wsClient.connect();
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("ping", {"message": "warmup", "user_id": userId});
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

  Future<void> _applyUserSettings(UserSettings us) async {
    final sn = ref.read(settingsProvider.notifier);
    final prefs = await SharedPreferences.getInstance();
    if (us.learnerLevel != null && us.learnerLevel!.trim().isNotEmpty) {
      sn.setLearnerLevel(us.learnerLevel!.trim());
      await prefs.setString('learner_level', us.learnerLevel!.trim());
    }
    if (us.depthPreference != null) sn.setDepthPreference(us.depthPreference!);
    if (us.newTopicAppetite != null) sn.setNewTopicAppetite(us.newTopicAppetite!);
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
      wsClient.sendCommand("update_politeness", {"level": level, "user_id": userId});
    } catch (_) {}
  }

  Future<void> speakText(String text) async {
    await forceIdle();
    state = state.copyWith(status: ChatStatus.speaking);
    debugPrint('[speakText] status -> speaking');
    try {
      _audio.startPcmStream();
      await _audio.startPlayerFromStream();
      debugPrint('[speakText] player started');
      final userId = await UserManager.getOrCreateUuid();
      ref.read(websocketProvider).sendCommand("request_tts", {"text": text, "user_id": userId});
    } catch (e) {
      debugPrint('[speakText] exception: $e');
      forceIdle();
    }
  }

  Future<void> startListening() async {
    debugPrint('[startListening] ▶ called, _isStartingListen=$_isStartingListen, status=${state.status}');
    // Fix B4: mutex guard
    if (_isStartingListen) {
      debugPrint('[startListening] guard: _isStartingListen=true, returning');
      return;
    }
    if (state.status == ChatStatus.listening) {
      debugPrint('[startListening] guard: already listening, returning');
      return;
    }
    _isStartingListen = true;

    try {
      await _audio.stopPlayer();
      debugPrint('[startListening] player stopped');
      final permStatus = await Permission.microphone.request();
      debugPrint('[startListening] mic permission=$permStatus');
      if (!permStatus.isGranted) {
        debugPrint('[startListening] mic denied, returning');
        return;
      }

      final wsClient = ref.read(websocketProvider);
      await wsClient.connect();

      final stream = await _audio.startRecording();
      debugPrint('[startListening] recording stream=${stream != null}');
      if (stream == null) {
        debugPrint('[startListening] stream null, returning');
        return;
      }

      state = state.copyWith(status: ChatStatus.listening);
      debugPrint('[startListening] status -> listening');
      _hasSpoken = false;
      _noiseFrames = 0;

      // 音频流发送到 WS
      stream.listen((data) {
        if (state.status == ChatStatus.listening) {
          wsClient.sendAudio(data);
        }
      });

      // Fix B5: 最大录音计时器（30s 静默保护）
      _maxListenTimer?.cancel();
      _maxListenTimer = Timer(const Duration(seconds: 30), () {
        if (state.status == ChatStatus.listening) {
          if (_hasSpoken) {
            stopListeningAndSubmit();
          } else {
            forceIdle();
          }
        }
      });

      // Fix B3: VAD（声音激活检测）
      _audio.recorder.onAmplitudeChanged(const Duration(milliseconds: 100))
          .listen((amp) {
        if (state.status != ChatStatus.listening) return;
        final currentVadTimeout = ref.read(settingsProvider).vadTimeout;
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
      debugPrint('[startListening] VAD listener registered');
    } catch (e, st) {
      debugPrint('[startListening] EXCEPTION: $e');
      debugPrint('[startListening] STACKTRACE: $st');
      forceIdle();
    } finally {
      debugPrint('[startListening] finally: _isStartingListen=false');
      _isStartingListen = false;
    }
  }

  Future<void> stopListeningAndSubmit() async {
    debugPrint('[stopListening] ▶ called, status=${state.status}');
    if (state.status != ChatStatus.listening) {
      debugPrint('[stopListening] not listening, returning');
      return;
    }
    _latencyTurnId = const Uuid().v4().replaceAll('-', '');
    _latencyFirstPcmMs = null;
    _latencyLoggedFirstPcm = false;
    _latencySw = Stopwatch()..start();
    _latencyLogClient('01_stopListening_submit_start');

    try {
      await _audio.stopRecording();
      _latencyLogClient('02_recorder_stopped');
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel();

      state = state.copyWith(status: ChatStatus.speaking);
      _audio.startPcmStream();
      await _audio.startPlayerFromStream();
      _latencyLogClient('03_player_stream_ready');

      // 订阅 PCM 流写入播放器
      _pcmSubscription?.cancel();
      _pcmSubscription = _audio.pcmStream?.listen(
        (bytes) {
          _audio.writeToPlayer(bytes);
        },
        onDone: () {
          debugPrint('[pcmSubscription] onDone: stream closed, calling _onAudioFinished');
          _onAudioFinished();
        },
      );

      final userId = await UserManager.getOrCreateUuid();
      final wallMs = DateTime.now().millisecondsSinceEpoch;
      ref.read(websocketProvider).sendCommand("user_finish_speaking", {
        "user_id": userId,
        "trace_id": _latencyTurnId,
        "client_submit_wall_ms": wallMs,
      });
      _latencyLogClient('04_ws_user_finish_sent');
    } catch (_) {
      _resetLatencyState();
      forceIdle();
    }
  }

  Future<void> forceIdle() async {
    _resetLatencyState();
    _isStartingListen = false;  // Fix B4
    _audio.resetPlaybackState();
    _pcmSubscription?.cancel();
    try {
      await _audio.stopPlayer();
    } catch (_) {}
    try {
      if (await _audio.checkIsRecording()) await _audio.stopRecording();
    } catch (_) {}
    _silenceTimer?.cancel();
    _maxListenTimer?.cancel();
    state = state.copyWith(status: ChatStatus.idle);
  }

  void _resetLatencyState() {
    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;
    _latencyLoggedFirstPcm = false;
  }

  /// 打断机制：AI 说话途中用户开口 → 立刻停播 + 通知后端 + 开始录音
  Future<void> interruptAndListen() async {
    if (state.status != ChatStatus.speaking) return;
    _isInterrupting = true;

    _audio.closePcmStream();
    await _audio.stopPlayer();
    _audio.resetPlaybackState();

    ref.read(websocketProvider).sendCommand("cancel_tts", {});

    _resetLatencyState();
    _isInterrupting = false;
    await startListening();
  }

  void clearError() {
    state = state.copyWith(errorMessage: null);
  }

  void clearSessionReport() {
    _reportShowing = false;
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

  Future<void> toggleButton() async {
    if (state.status == ChatStatus.idle) {
      await startListening();
    } else if (state.status == ChatStatus.listening) {
      await stopListeningAndSubmit();
    } else if (state.status == ChatStatus.speaking) {
      await interruptAndListen();
    }
  }

  // ── Latency 工具 ─────────────────────────────────────────────────

  void _latencyLogClient(String stage, [String extra = '']) {
    final sw = _latencySw;
    if (sw == null) return;
    final ms = sw.elapsedMilliseconds;
    final tail = extra.isEmpty ? '' : ' $extra';
    final tid = _latencyTurnId;
    final turnSeg = (tid != null && tid.isNotEmpty) ? 'turn=$tid ' : '';
    debugPrint('[LATENCY][client] $turnSeg$stage +${ms}ms$tail');
  }
}
