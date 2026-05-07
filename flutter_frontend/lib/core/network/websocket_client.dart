import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../logging/app_logger.dart';
import 'backend_config.dart';

// ── 连接状态枚举 ────────────────────────────────────────────────────────────
enum WsConnectionState { disconnected, connecting, connected, reconnecting }

// ── Providers ───────────────────────────────────────────────────────────────
final websocketProvider = Provider<WebSocketClient>((ref) {
  final client = WebSocketClient();
  ref.onDispose(() => client.dispose());
  return client;
});

/// 将 WebSocketClient 的连接状态暴露为 StreamProvider，供 chat_provider 监听
final wsConnectionStateProvider = StreamProvider<WsConnectionState>((ref) {
  return ref.watch(websocketProvider).connectionStateStream;
});

// ── WebSocketClient ──────────────────────────────────────────────────────────
class WebSocketClient {
  WebSocketChannel? _channel;

  // 使用 broadcast 流来分发高频的 JSON 消息，包括 ai_text_stream
  final _jsonController = StreamController<Map<String, dynamic>>.broadcast();
  final _audioController = StreamController<Uint8List>.broadcast();
  final _stateController = StreamController<WsConnectionState>.broadcast();

  Timer? _heartbeatTimer;
  Timer? _reconnectTimer;

  int _reconnectAttempts = 0;
  bool _intentionalDisconnect = false;
  WsConnectionState _state = WsConnectionState.disconnected;
  String? _userId;

  // ── 配置常量 ──────────────────────────────────────────────────────────────
  static String get _url => kBackendWsUrl;
  static const int _maxReconnectAttempts = 5;
  static const Duration _heartbeatInterval = Duration(seconds: 20);

  WsConnectionState get connectionState => _state;
  Stream<Map<String, dynamic>> get commandStream => _jsonController.stream;
  Stream<Uint8List> get audioStream => _audioController.stream;
  Stream<WsConnectionState> get connectionStateStream =>
      _stateController.stream;

  /// 设置 user_id，后续 connect() 时会作为查询参数传递
  void setUserId(String? userId) {
    _userId = userId;
  }

  String? get userId => _userId;

  // ── 连接 ─────────────────────────────────────────────────────────────────
  Future<void> connect() async {
    if (_state == WsConnectionState.connected ||
        _state == WsConnectionState.connecting) {
      return;
    }
    _intentionalDisconnect = false;
    _setState(WsConnectionState.connecting);

    String fullUrl = _url;
    if (_userId != null) {
      fullUrl = '$_url?user_id=$_userId';
    }
    AppLogger.log('WS', 'Connecting to $fullUrl');

    try {
      _channel = WebSocketChannel.connect(Uri.parse(fullUrl));
      await _channel!.ready.timeout(
        const Duration(seconds: 8),
        onTimeout: () {
          _channel?.sink.close(1008, 'connection timeout');
          throw TimeoutException('WebSocket handshake timeout');
        },
      );
      _channel!.stream.listen(
        _onMessage,
        onError: (error) => _onLostConnection('error: $error'),
        onDone: () => _onLostConnection('server closed'),
      );
      _setState(WsConnectionState.connected);
      AppLogger.log('WS', 'Connected successfully');
      _reconnectAttempts = 0;
      _startHeartbeat();
    } catch (e) {
      AppLogger.instance.error('[WS] Connection failed: $e');
      _setState(WsConnectionState.disconnected);
      _scheduleReconnect();
    }
  }

  // ── 消息分发 ─────────────────────────────────────────────────────────────
  void _onMessage(dynamic message) {
    if (message is String) {
      try {
        _jsonController.add(jsonDecode(message) as Map<String, dynamic>);
      } catch (_) {
        // 忽略格式错误的 JSON
      }
    } else if (message is List<int>) {
      _audioController.add(Uint8List.fromList(message));
    }
  }

  // ── 断线处理 ─────────────────────────────────────────────────────────────
  void _onLostConnection(String reason) {
    _stopHeartbeat();
    _channel = null;
    if (_intentionalDisconnect) {
      _setState(WsConnectionState.disconnected);
      return;
    }
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_reconnectAttempts >= _maxReconnectAttempts) {
      AppLogger.instance.error('[WS] Max reconnect attempts reached');
      _setState(WsConnectionState.disconnected);
      return;
    }
    _setState(WsConnectionState.reconnecting);
    // 指数退避：1s → 2s → 4s → 8s → 16s
    final delaySeconds = 1 << _reconnectAttempts;
    AppLogger.log('WS', 'Scheduling reconnect attempt ${_reconnectAttempts + 1} in ${delaySeconds}s');
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(Duration(seconds: delaySeconds), () {
      _reconnectAttempts++;
      connect();
    });
  }

  // ── 心跳 ─────────────────────────────────────────────────────────────────
  void _startHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = Timer.periodic(_heartbeatInterval, (_) {
      if (_state == WsConnectionState.connected) {
        sendCommand("ping", {});
      }
    });
  }

  void _stopHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;
  }

  // ── 发送 ─────────────────────────────────────────────────────────────────
  void sendCommand(String action, Map<String, dynamic> data) {
    if (_channel != null && _state == WsConnectionState.connected) {
      _channel!.sink.add(jsonEncode({"action": action, ...data}));
    }
  }

  void sendAudio(List<int> audioData) {
    if (_channel != null && _state == WsConnectionState.connected) {
      _channel!.sink.add(audioData);
    }
  }

  // ── 主动断开 ─────────────────────────────────────────────────────────────
  void disconnect() {
    _intentionalDisconnect = true;
    AppLogger.log('WS', 'Intentional disconnect');
    _stopHeartbeat();
    _reconnectTimer?.cancel();
    _channel?.sink.close(1001);
    _channel = null;
    _setState(WsConnectionState.disconnected);
  }

  void _setState(WsConnectionState newState) {
    _state = newState;
    if (!_stateController.isClosed) _stateController.add(newState);
  }

  void dispose() {
    disconnect();
    _jsonController.close();
    _audioController.close();
    _stateController.close();
  }
}
