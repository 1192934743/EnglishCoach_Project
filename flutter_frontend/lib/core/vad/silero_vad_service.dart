// lib/core/vad/silero_vad_service.dart
import 'dart:async';
import 'dart:typed_data';
import 'package:flutter/foundation.dart';
import 'package:vad/vad.dart';

class SileroVadService {
  VadIterator? _vad;
  bool _isInitialized = false;
  int? _currentThresholdMs;

  bool _isSpeaking = false;
  VoidCallback? onSpeechStart;
  VoidCallback? onSpeechEnd;
  Completer<void>? _inferLock;

  // 👇 新增：初始化锁，防止并发双重加载
  Future<void>? _initFuture;

  /// 外部调用的初始化入口
  Future<void> initialize({int silenceThresholdMs = 700}) async {
    // 1. 命中缓存：已经初始化完毕，且阈值没变，瞬间秒开
    if (_isInitialized && _currentThresholdMs == silenceThresholdMs) return;

    // 2. 命中排队：如果后台正好有一个初始化任务正在跑
    if (_initFuture != null) {
      debugPrint('[SileroVadService] 模型正在后台加载中，加入排队等待...');
      await _initFuture; // 等待那个正在跑的任务完成

      // 等待结束后，如果加载好的阈值就是我们要的，直接秒开
      if (_currentThresholdMs == silenceThresholdMs) return;
    }

    // 3. 执行真正的加载逻辑，并将这个 Future 赋值给锁
    _initFuture = _performInitialization(silenceThresholdMs);
    await _initFuture;
    _initFuture = null; // 加载完成后释放锁
  }

  /// 真正的底层加载逻辑（被锁保护）
  Future<void> _performInitialization(int silenceThresholdMs) async {
    if (_isInitialized) {
      debugPrint('[SileroVadService] 阈值变更，正在释放旧模型...');
      await _vad?.release();
      _isInitialized = false;
    }

    _currentThresholdMs = silenceThresholdMs;
    int redemptionFrames = (silenceThresholdMs / 32).ceil();

    try {
      _vad = await VadIterator.create(
        isDebug: false,
        sampleRate: 16000,
        frameSamples: 512,
        positiveSpeechThreshold: 0.5,
        negativeSpeechThreshold: 0.35,
        redemptionFrames: redemptionFrames,
        preSpeechPadFrames: 1,
        minSpeechFrames: 3,
        model: 'v4',
      );

      _vad!.setVadEventCallback((VadEvent event) {
        final typeStr = event.type.toString().toUpperCase();
        if (typeStr.contains('START') || typeStr.contains('REALSTART')) {
          if (!_isSpeaking) {
            _isSpeaking = true;
            onSpeechStart?.call();
          }
        } else if (typeStr.contains('END')) {
          if (_isSpeaking) {
            _isSpeaking = false;
            onSpeechEnd?.call();
          }
        }
      });

      _isInitialized = true;
      debugPrint(
        '[SileroVadService] Initialized with timeout: ${silenceThresholdMs}ms ($redemptionFrames frames)',
      );
    } catch (e) {
      debugPrint('[SileroVadService] Init Error: $e');
    }
  }

  void setCallbacks({VoidCallback? onStart, VoidCallback? onEnd}) {
    onSpeechStart = onStart;
    onSpeechEnd = onEnd;
  }

  void feedPCM(Uint8List data) async {
    if (!_isInitialized || _vad == null) return;

    while (_inferLock != null) {
      await _inferLock!.future;
    }
    _inferLock = Completer<void>();

    try {
      await _vad!.processAudioData(data);
    } catch (e) {
      debugPrint('[SileroVadService] feedPCM Error: $e');
    } finally {
      _inferLock?.complete();
      _inferLock = null;
    }
  }

  void reset() {
    _isSpeaking = false;
    _vad?.reset();
  }

  void stopListening() {
    reset();
  }
}
