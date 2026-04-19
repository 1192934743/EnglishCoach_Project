// 音频录制与播放控制器。
//
// 封装 AudioRecorder / FlutterSoundPlayer 的使用，提供统一的录制/播放接口。
// 由 ChatNotifier 持有，录音/播放的生命周期委托给此类处理。

import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:record/record.dart';
import 'package:flutter_sound/flutter_sound.dart';
import 'package:audio_session/audio_session.dart';

class AudioController {
  AudioRecorder? _recorder;
  final FlutterSoundPlayer _player = FlutterSoundPlayer();

  bool _isRecording = false;
  bool _isPlaying = false;
  int _totalBytesReceived = 0;
  int _lastTotalBytes = 0;   // 上一次播放的总字节数（用于 ttsFinished 时估算音频时长）
  DateTime? _playbackStartTime;

  StreamController<Uint8List>? _pcmStreamController;
  StreamSubscription? _recordStreamSubscription;

  bool get isRecording => _isRecording;
  bool get isPlaying => _isPlaying;
  int get totalBytesReceived => _totalBytesReceived;
  int get lastTotalBytes => _lastTotalBytes;
  DateTime? get playbackStartTime => _playbackStartTime;
  FlutterSoundPlayer get player => _player;
  AudioRecorder get recorder {
    _recorder ??= AudioRecorder();
    return _recorder!;
  }

  /// 初始化音频会话和播放器
  Future<void> init() async {
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
          androidWillPauseWhenDucked: false,
        ),
      );
      await _player.openPlayer();
    } catch (e) {
      debugPrint("音频会话配置异常: $e");
    }
  }

  /// 打开 PCM 流（供音频订阅者消费）
  void startPcmStream() {
    _pcmStreamController?.close();
    _pcmStreamController = StreamController<Uint8List>.broadcast();
    _totalBytesReceived = 0;
    _playbackStartTime = null;
  }

  Stream<Uint8List>? get pcmStream => _pcmStreamController?.stream;

  /// 写入 PCM 数据（WS 音频流回调调用）
  void writePcm(Uint8List bytes) {
    if (_pcmStreamController == null || _pcmStreamController!.isClosed) {
      debugPrint('[AudioCtrl] writePcm: stream closed, dropping ${bytes.length} bytes');
      return;
    }
    _totalBytesReceived += bytes.length;
    _playbackStartTime ??= DateTime.now();
    _pcmStreamController!.add(bytes);
  }

  /// 关闭 PCM 流
  void closePcmStream() {
    debugPrint('[AudioCtrl] closePcmStream called, isClosed=${_pcmStreamController?.isClosed}');
    _pcmStreamController?.close();
    _pcmStreamController = null;
    debugPrint('[AudioCtrl] pcmStream closed and set to null');
  }

  /// 开始播放音频流（从 PCM stream）
  Future<void> startPlayerFromStream({
    Codec codec = Codec.pcm16,
    int numChannels = 1,
    int sampleRate = 24000,
    int bufferSize = 8192,
  }) async {
    debugPrint('[AudioCtrl] startPlayerFromStream sampleRate=$sampleRate');
    if (_player.isPlaying) {
      debugPrint('[AudioCtrl] already playing, stopping first');
      await _player.stopPlayer();
    }
    await _player.startPlayerFromStream(
      codec: codec,
      numChannels: numChannels,
      sampleRate: sampleRate,
      interleaved: true,
      bufferSize: bufferSize,
      onBufferUnderflow: () {
        debugPrint('[AudioCtrl] onBufferUnderflow (normal during streaming, ignoring)');
      },
    );
    _isPlaying = true;
    debugPrint('[AudioCtrl] player started, _isPlaying=$_isPlaying');
  }

  /// 写入 PCM 数据到播放器（同步写入，让音频流保持流畅）
  void writeToPlayer(Uint8List bytes) {
    if (_player.uint8ListSink == null) {
      debugPrint('[AudioCtrl] writeToPlayer: sink is null, dropping ${bytes.length} bytes');
      return;
    }
    try {
      _player.uint8ListSink?.add(bytes);
    } catch (e) {
      debugPrint('[AudioCtrl] writeToPlayer EXCEPTION: $e');
    }
  }

  /// 启动播放进度监听器（供 NativeCallback 模式使用）。
  /// 返回的 StreamSubscription 在关闭流时要 cancel。
  StreamSubscription<PlaybackDisposition>? startPlaybackProgressListener(
    void Function(int receivedBytes) onProgress,
  ) {
    _player.setSubscriptionDuration(const Duration(milliseconds: 200));
    return _player.onProgress?.listen((disp) {
      // disp.position 是已播放时长，disp.duration 对于流可能不准，
      // 所以用 receivedBytes 配合音频码率来估算。
      onProgress(_totalBytesReceived);
    });
  }

  /// 获取本次播放收到的音频总字节数（供调试用）
  int get totalPcmBytes => _totalBytesReceived;

  /// 停止播放器
  Future<void> stopPlayer() async {
    debugPrint('[AudioCtrl] stopPlayer called, _isPlaying=$_isPlaying, player.isPlaying=${_player.isPlaying}');
    try {
      if (_player.isPlaying) await _player.stopPlayer();
    } catch (e) {
      debugPrint('[AudioCtrl] stopPlayer exception: $e');
    }
    _isPlaying = false;
    debugPrint('[AudioCtrl] stopPlayer done');
  }

  /// 关闭播放器
  Future<void> closePlayer() async {
    await _player.closePlayer();
  }

  /// 打开播放器内部的 StreamSink（用于 tts_finished 时关闭）
  StreamSink<Uint8List>? get playerSink => _player.uint8ListSink;

  /// 开始录制音频流，返回 Stream<Uint8List>
  Future<Stream<Uint8List>?> startRecording({
    RecordConfig config = const RecordConfig(
      encoder: AudioEncoder.pcm16bits,
      sampleRate: 16000,
      numChannels: 1,
    ),
  }) async {
    _isRecording = false;
    if (await recorder.isRecording()) await recorder.stop();
    final stream = await recorder.startStream(config);
    _isRecording = true;
    return stream;
  }

  /// 停止录制
  Future<void> stopRecording() async {
    if (!await recorder.isRecording()) return;
    await recorder.stop();
    _isRecording = false;
  }

  /// 是否正在录制
  Future<bool> checkIsRecording() async {
    return recorder.isRecording();
  }

  /// 释放录音器资源
  Future<void> disposeRecorder() async {
    _recordStreamSubscription?.cancel();
    _recordStreamSubscription = null;
    if (_isRecording) await recorder.stop();
    await recorder.dispose();
    _recorder = null;
    _isRecording = false;
  }

  /// 重置播放状态
  void resetPlaybackState() {
    _lastTotalBytes = _totalBytesReceived;  // 保存用于下次估算时长
    _totalBytesReceived = 0;
    _playbackStartTime = null;
  }

  /// 释放所有资源
  Future<void> dispose() async {
    await disposeRecorder();
    await closePlayer();
    await _pcmStreamController?.close();
    _pcmStreamController = null;
  }
}
