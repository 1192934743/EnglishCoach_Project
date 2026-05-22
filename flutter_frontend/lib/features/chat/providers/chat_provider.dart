// lib/features/chat/providers/chat_provider.dart
import 'dart:async';
import 'dart:typed_data';
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
import '../../../core/vad/silero_vad_service.dart';
import '../../../core/config/dev_panel_config.dart';
import '../models/session_report_model.dart';
import '../../topics/models/topic_item.dart'; // ← 【阶段四新增】TopicItem 模型

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
  final SessionReport? sessionReport;
  final String currentTopicTitle;
  final String currentTopicTitleZh;
  final int? currentTopicId; // ← 【阶段四新增】用于精准重复检测
  final String currentRoleName;
  final bool isGeneratingTopic;
  final bool isWaitingForTeachingData;

  // ── 【阶段三新增】微场景流转 UI 状态 ────────────────────────────────
  // 当前微场景名称（如 "Confirm Cup Size"），空字符串表示未启用微场景模式
  final String currentScenarioName;
  // 当前微场景的教学意图描述
  final String currentIntent;

  ChatState({
    required this.status,
    required this.chatHistory,
    this.isFlipped = false,
    this.masteryProgress = 0.0,
    this.errorMessage,
    this.sessionReport,
    this.currentTopicTitle = "Simulation Practice",
    this.currentTopicTitleZh = '',
    this.currentTopicId, // ← 【阶段四新增】
    this.currentRoleName = "AI Coach",
    this.isGeneratingTopic = false,
    this.isWaitingForTeachingData = false,
    this.currentScenarioName = '',
    this.currentIntent = '',
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
    int? currentTopicId, // ← 【阶段四新增】
    String? currentRoleName,
    bool? isGeneratingTopic,
    bool? isWaitingForTeachingData,
    String? currentScenarioName,
    String? currentIntent,
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
      currentTopicId: currentTopicId ?? this.currentTopicId, // ← 【阶段四新增】
      currentRoleName: currentRoleName ?? this.currentRoleName,
      isGeneratingTopic: isGeneratingTopic ?? this.isGeneratingTopic,
      isWaitingForTeachingData:
          isWaitingForTeachingData ?? this.isWaitingForTeachingData,
      currentScenarioName: currentScenarioName ?? this.currentScenarioName,
      currentIntent: currentIntent ?? this.currentIntent,
    );
  }
}

const Object _sentinel = Object();

class ChatNotifier extends Notifier<ChatState> {
  late AudioRecorder _recorder;
  final FlutterSoundPlayer _player = FlutterSoundPlayer();

  // VAD 服务实例
  late SileroVadService _vadService;

  StreamSubscription? _commandSubscription;
  StreamSubscription? _audioSubscription;
  StreamSubscription? _connectionSubscription;
  Timer? _silenceTimer;

  bool _hasSpoken = false;
  bool _isAutoLooping = false;
  int _totalBytesReceived = 0;
  DateTime? _playbackStartTime;

  bool _isInterrupting = false;
  bool _wasReconnecting = false;
  bool _isStartingListen = false;
  bool _isRequestingTopic = false; // ← 【阶段四新增】防抖标志
  Timer? _maxListenTimer;
  bool _reportShowing = false;

  Stopwatch? _latencySw;
  bool _latencyLoggedFirstPcm = false;
  String? _latencyTurnId;
  int? _latencyFirstPcmMs;
  DateTime? _lastPcmReceiveTime;

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
    _vadService = SileroVadService(); // 初始化 VAD 服务

    _initAudioSessionAndPlayer();
    _initWebSocketListeners();
    warmUpConnection();

    final wsClient = ref.read(websocketProvider);
    _connectionSubscription = wsClient.connectionStateStream.listen((
      connState,
    ) {
      debugPrint('[WS] connectionState → $connState  (status=${state.status})');
      if (connState == WsConnectionState.reconnecting) {
        _wasReconnecting = true;
        forceIdle();
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
      _connectionSubscription?.cancel();
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel();
      _recorder.dispose();
      _player.closePlayer();
      _vadService.stopListening();
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
    await ref.read(settingsProvider.notifier).ensureInitialized();
    final settings = ref.read(settingsProvider);

    debugPrint('[Warmup] 🚀 开始并行预热引擎...');

    await Future.wait([
      // 任务1：VAD 纯本地加载，极少失败，但加上兜底
      _vadService
          .initialize(silenceThresholdMs: settings.vadTimeout)
          .catchError((e) {
            debugPrint('[Warmup] ⚠️ VAD 初始化异常: $e');
          }),

      // 任务2：音频系统配置
      _initAudioSessionAndPlayer().catchError((e) {
        debugPrint('[Warmup] ⚠️ AudioSession 异常: $e');
      }),

      // 任务3：WebSocket 建连（最容易因为网络波动）
      Future<void>(() async {
        final userId = await UserManager.getOrCreateUuid();
        final wsClient = ref.read(websocketProvider);
        wsClient.setUserId(userId);
        wsClient.connect().catchError((e) {
          debugPrint('[Warmup] ⚠️ WebSocket 初始连线失败 (将由断线重连机制接管): $e');
        });
      }),
    ]);

    debugPrint('[Warmup] ✅ 底层并行加载结束');

    // Fire and forget: 同步 LMS 设置
    _syncLmsSettings();
  }

  Future<void> _syncLmsSettings() async {
    try {
      final wsClient = ref.read(websocketProvider);
      final userId = await UserManager.getOrCreateUuid();
      wsClient.sendCommand("ping", {"message": "warmup", "user_id": userId});
      await Future<void>.delayed(const Duration(milliseconds: 200));
      final settings = ref.read(settingsProvider);
      final prefs = await SharedPreferences.getInstance();
      final skipTts = prefs.getBool('skip_tts') ?? false;
      wsClient.sendCommand("update_lms_settings", {
        "user_id": userId,
        "depth_preference": settings.depthPreference,
        "new_topic_appetite": settings.newTopicAppetite,
        "learner_level": settings.learnerLevel,
        "tts_engine": settings.ttsEngine,
        "tts_voice": settings.ttsVoice,
        "skip_tts": skipTts,
      });
    } catch (_) {}
  }

  Future<void> _applyUserSettingsFromServer(Object? raw) async {
    if (raw is! Map) return;
    final us = Map<String, dynamic>.from(raw);
    if (us.isEmpty) return;
    final sn = ref.read(settingsProvider.notifier);
    final prefs = await SharedPreferences.getInstance();

    final currentLv = ref.read(settingsProvider).learnerLevel;
    final defaultLv = "Intermediate";
    if (currentLv == defaultLv) {
      final lv = us['learner_level'];
      if (lv is String && lv.trim().isNotEmpty) {
        sn.setLearnerLevel(lv.trim());
        await prefs.setString('learner_level', lv.trim());
      }
    }

    final currentDp = ref.read(settingsProvider).depthPreference;
    if (currentDp == 1.0) {
      final dp = us['depth_preference'];
      if (dp is num) {
        sn.setDepthPreference(dp.toDouble());
      }
    }

    final currentAp = ref.read(settingsProvider).newTopicAppetite;
    if (currentAp == 0.2) {
      final ap = us['new_topic_appetite'];
      if (ap is num) {
        sn.setNewTopicAppetite(ap.toDouble());
      }
    }
  }

  Future<void> swapRole() async {
    final wsClient = ref.read(websocketProvider);
    try {
      final userId = await UserManager.getOrCreateUuid();
      wsClient.setUserId(userId);
      await wsClient.connect();
      wsClient.sendCommand("swap_role", {"user_id": userId});
    } catch (_) {}
  }

  Future<void> updatePoliteness(int level) async {
    final wsClient = ref.read(websocketProvider);
    try {
      final userId = await UserManager.getOrCreateUuid();
      wsClient.setUserId(userId);
      await wsClient.connect();
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
          // 强行呼起 iOS 硬件降噪与 AEC
          avAudioSessionMode: AVAudioSessionMode.voiceChat,
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
        bufferSize: 96000,
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
    _commandSubscription = wsClient.commandStream.listen((data) async {
      if (data['event'] == 'warmup_success') {
        await _applyUserSettingsFromServer(data['user_settings']);
      } else if (data['event'] == 'tts_finished') {
        if (_isInterrupting) return;
        _handleAudioFinished();
      } else if (data['event'] == 'asr_partial') {
        activeUserTextNotifier.value = data['text'] ?? '';
      } else if (data['event'] == 'ai_text_stream') {
        if (!state.isWaitingForTeachingData) {
          state = state.copyWith(isWaitingForTeachingData: true);
        }
        activeAiTextNotifier.value += (data['text'] ?? '');
      } else if (data['event'] == 'teaching_data') {
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
          currentTopicId: data['topic_id'] as int?, // ← 【阶段四新增】
          currentRoleName:
              data['role_name'] as String? ?? state.currentRoleName,
          masteryProgress: 0.0,
          // topic_changed 时同时重置微场景状态（降级为话题级展示）
          currentScenarioName: '',
          currentIntent: '',
        );
      } else if (data['event'] == 'scenario_transition') {
        // ── 【阶段三新增】微场景图谱流转事件 ──────────────────────────
        // 当用户在当前微场景中命中了所有约束时，后端下发此事件，
        // 通知前端切换到下一个微场景，同时附带教学意图描述。
        // 注意：本事件仅更新状态，不做 UI 副作用（UI 副作用由 ChatScreen
        // 中的 ref.listen 统一处理，严格遵守 Riverpod 纯状态 + 视图副作用分离原则）。
        state = state.copyWith(
          currentScenarioName:
              data['new_scenario_name'] as String? ?? state.currentScenarioName,
          currentIntent: data['new_intent'] as String? ?? state.currentIntent,
          isWaitingForTeachingData: false,
        );
        // 【修复】清除 AI 先手流式累积文字，避免残留
        activeAiTextNotifier.value = '';
        activeUserTextNotifier.value = '';
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
            : (en ?? (isZh ? '出错了，请稍后再试。' : 'Something went wrong.'));
        state = state.copyWith(errorMessage: message);
      }
    }, onError: (_) => forceIdle());

    _audioSubscription = wsClient.audioStream.listen((audioBytes) {
      if (_isInterrupting) return;
      if (state.status == ChatStatus.speaking && _player.isPlaying) {
        final now = DateTime.now();
        if (_lastPcmReceiveTime != null) {
          final intervalMs = now
              .difference(_lastPcmReceiveTime!)
              .inMilliseconds;
          if (intervalMs > 100) {
            debugPrint('[TRACK_AUDIO] 收包间隔过大 interval=${intervalMs}ms');
          }
        }
        _lastPcmReceiveTime = now;

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

    // 【阶段四修复】重置 _isStartingListen，允许后续 startListening
    _isStartingListen = false;

    // 【阶段四修复】AI 先手场景下重置 isWaitingForTeachingData
    // 因为跳过了 Director 评估，不会有 teaching_data 事件来重置它
    if (state.isWaitingForTeachingData) {
      state = state.copyWith(isWaitingForTeachingData: false);
    }

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

    // 【阶段四修复】等待音频播放完成，而不是立即关闭
    // 估算音频时长并等待（字节率 = 24000Hz * 1ch * 2bytes = 48000 bytes/sec）
    // 增加 1000ms 缓冲余量，防止估算偏短导致切换过早
    const int bufferMs = 0;
    if (_playbackStartTime != null && _totalBytesReceived > 0) {
      int durationMs = (_totalBytesReceived / 48.0).ceil() + bufferMs;
      int elapsedMs = DateTime.now()
          .difference(_playbackStartTime!)
          .inMilliseconds;
      int timeLeftMs = durationMs - elapsedMs;
      if (timeLeftMs > 0) {
        debugPrint(
          '[AI_FIRST_STRIKE] 等待音频播放完成，还需 ${timeLeftMs}ms (原始估算: ${(durationMs - bufferMs)}ms + 缓冲 $bufferMs)',
        );
        await Future.delayed(Duration(milliseconds: timeLeftMs));
      }
    }

    // 确保播放器完全停止后再清理
    try {
      if (_player.isPlaying) {
        await _player.stopPlayer();
        // 给播放器一点时间完全停止
        await Future.delayed(const Duration(milliseconds: 100));
      }
    } catch (_) {}

    _isAutoLooping = false;
    _playbackStartTime = null;
    _totalBytesReceived = 0;
    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;

    if (ref.read(settingsProvider).autoMode && !_reportShowing) {
      startListening();
    } else {
      state = state.copyWith(status: ChatStatus.idle);
    }
  }

  Future<void> startListening() async {
    if (_isStartingListen) return;
    if (state.status == ChatStatus.listening) return;
    _isStartingListen = true;

    try {
      if (_player.isPlaying) await _player.stopPlayer();

      final permStatus = await Permission.microphone.request();
      if (!permStatus.isGranted) {
        _isStartingListen = false;
        return;
      }

      final wsClient = ref.read(websocketProvider);
      // 不阻塞主线程连接，后台自动保障
      final userId = await UserManager.getOrCreateUuid();
      wsClient.setUserId(userId);
      wsClient.connect();

      // 强力激活 Android/iOS 底层硬件降噪
      const config = RecordConfig(
        encoder: AudioEncoder.pcm16bits,
        sampleRate: 16000,
        numChannels: 1,
        echoCancel: true,
        noiseSuppress: true,
        autoGain: true,
      );

      if (await _recorder.isRecording()) await _recorder.stop();

      // 这里现在是秒开，如果是同样阈值，直接 return
      final currentVadTimeout = ref.read(settingsProvider).vadTimeout;
      await _vadService.initialize(silenceThresholdMs: currentVadTimeout);

      _vadService.setCallbacks(
        onStart: () {
          if (state.status == ChatStatus.listening) {
            _hasSpoken = true;
            _silenceTimer?.cancel();
            debugPrint('[SileroVAD] - 人类开始说话');
          }
        },
        onEnd: () {
          if (state.status == ChatStatus.listening && _hasSpoken) {
            debugPrint('[SileroVAD] - 人类语音中断，触发自动发送');
            stopListeningAndSubmit();
          }
        },
      );

      final stream = await _recorder.startStream(config);
      final broadcastStream = stream.asBroadcastStream();

      state = state.copyWith(status: ChatStatus.listening);
      _hasSpoken = false;

      broadcastStream.listen((data) {
        if (state.status == ChatStatus.listening) {
          wsClient.sendAudio(data);
          _vadService.feedPCM(data);
        }
      });
    } catch (e, st) {
      debugPrint('[startListening] EXCEPTION: $e');
      debugPrint('[startListening] STACKTRACE: $st');
      forceIdle();
    } finally {
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
    _lastPcmReceiveTime = null;

    try {
      await _recorder.stop();
      _vadService.stopListening(); // 停止 VAD 监听

      _latencyLogClient('02_recorder_stopped');
      _silenceTimer?.cancel();
      _maxListenTimer?.cancel();

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
        bufferSize: 96000,
      );
      _latencyLogClient('03_player_stream_ready');
      final userId = await UserManager.getOrCreateUuid();

      // 构建消息，开发者模式携带 debug 参数
      final message = <String, dynamic>{
        "user_id": userId,
        "trace_id": _latencyTurnId,
        "client_submit_wall_ms": DateTime.now().millisecondsSinceEpoch,
      };

      // 检查开发者模式
      final prefs = await SharedPreferences.getInstance();
      final devModeEnabled = prefs.getBool(DevPanelConfig.devModeKey) ?? false;
      if (devModeEnabled) {
        final debugModel = prefs.getString(DevPanelConfig.llmModelKey);
        if (debugModel != null && debugModel.isNotEmpty) {
          message["debug"] = {"model": debugModel};
        }
      }

      ref.read(websocketProvider).sendCommand("user_finish_speaking", message);
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
    _isStartingListen = false;
    _playbackStartTime = null;
    _totalBytesReceived = 0;
    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (_) {}
    try {
      if (await _recorder.isRecording()) await _recorder.stop();
    } catch (_) {}

    try {
      _vadService.stopListening(); // 清理模型内部状态
    } catch (_) {}

    _silenceTimer?.cancel();
    _maxListenTimer?.cancel();

    state = state.copyWith(
      status: ChatStatus.idle,
      isWaitingForTeachingData: false,
    );
    activeUserTextNotifier.value = '';
    activeAiTextNotifier.value = '';
  }

  Future<void> interruptAndListen() async {
    if (state.status != ChatStatus.speaking) return;

    _isInterrupting = true;
    _isAutoLooping = false;

    try {
      await _player.uint8ListSink?.close();
    } catch (_) {}

    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (_) {}

    _totalBytesReceived = 0;
    _playbackStartTime = null;
    _lastPcmReceiveTime = null;

    ref.read(websocketProvider).sendCommand("cancel_tts", {});

    _latencySw = null;
    _latencyTurnId = null;
    _latencyFirstPcmMs = null;
    _latencyLoggedFirstPcm = false;

    state = state.copyWith(isWaitingForTeachingData: false);
    activeUserTextNotifier.value = '';
    activeAiTextNotifier.value = '';

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

  Future<void> requestTopic(TopicItem topic) async {
    if (_isRequestingTopic) return; // 防抖

    // 精准重复检测：使用 topic.id
    if (state.currentTopicId == topic.id) {
      return;
    }

    _isRequestingTopic = true;
    state = state.copyWith(isGeneratingTopic: true);

    final wsClient = ref.read(websocketProvider);
    try {
      final userId = await UserManager.getOrCreateUuid();
      wsClient.setUserId(userId);
      await wsClient.connect();
      wsClient.sendCommand("request_topic", {
        "user_id": userId,
        "description": topic.title,
        "topic_id": topic.id, // ← 【阶段四新增】发送给后端
      });
    } catch (_) {
      _isRequestingTopic = false;
      state = state.copyWith(isGeneratingTopic: false);
    }
  }

  void onTopicChanged() {
    _isRequestingTopic = false;
  }

  // ── 【阶段四新增】AI 先手开场 ───────────────────────────────────
  // 注意：不设置为 listening，避免触发麦克风波纹 UI
  Future<void> triggerAiFirstStrike() async {
    if (state.status != ChatStatus.idle) return;
    if (_isStartingListen) return;

    _isStartingListen = true;
    _latencySw = Stopwatch()..start();
    _latencyLoggedFirstPcm = false;
    _totalBytesReceived = 0;
    _playbackStartTime = DateTime.now();
    _latencyTurnId = const Uuid().v4().replaceAll('-', '');

    // 设置为 speaking，让 UI 显示等待动画而不是麦克风录音
    // 并启动播放器接收 TTS 流
    state = state.copyWith(
      status: ChatStatus.speaking,
      isWaitingForTeachingData: true,
    );

    try {
      await _player.startPlayerFromStream(
        codec: Codec.pcm16,
        numChannels: 1,
        sampleRate: 24000,
        interleaved: true,
        bufferSize: 96000,
      );
      _latencyLogClient('03_player_stream_ready');
      // 给播放器一点时间确保完全准备好
      await Future.delayed(const Duration(milliseconds: 50));
    } catch (_) {
      _isStartingListen = false;
      forceIdle();
      return;
    }

    final wsClient = ref.read(websocketProvider);
    wsClient.sendCommand("test_text_input", {
      "text": "",
      "is_ai_first_strike": true,
    });
  }

  Future<void> updateLmsSettings({
    required double depthPreference,
    required double newTopicAppetite,
    required String learnerLevel,
    required String ttsEngine,
    required String ttsVoice,
  }) async {
    final wsClient = ref.read(websocketProvider);
    try {
      final userId = await UserManager.getOrCreateUuid();
      wsClient.setUserId(userId);
      await wsClient.connect();
      final prefs = await SharedPreferences.getInstance();
      final skipTts = prefs.getBool('skip_tts') ?? false;
      wsClient.sendCommand("update_lms_settings", {
        "user_id": userId,
        "depth_preference": depthPreference,
        "new_topic_appetite": newTopicAppetite,
        "learner_level": learnerLevel,
        "tts_engine": ttsEngine,
        "tts_voice": ttsVoice,
        "skip_tts": skipTts,
      });
    } catch (_) {}
  }

  void _handleSessionReport(Map<String, dynamic> data) {
    final stage = data['stage'] as String? ?? 'preliminary';

    if (stage == 'preliminary') {
      _reportShowing = true;
      forceIdle();

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
      await interruptAndListen();
    }
  }
}

final chatProvider = NotifierProvider<ChatNotifier, ChatState>(
  () => ChatNotifier(),
);
