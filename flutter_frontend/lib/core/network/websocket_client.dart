import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

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

  final _jsonController = StreamController<Map<String, dynamic>>.broadcast();
  final _audioController = StreamController<Uint8List>.broadcast();
  final _stateController = StreamController<WsConnectionState>.broadcast();

  Timer? _heartbeatTimer;
  Timer? _reconnectTimer;

  int _reconnectAttempts = 0;
  bool _intentionalDisconnect = false;
  WsConnectionState _state = WsConnectionState.disconnected;

  // ── 配置常量 ──────────────────────────────────────────────────────────────
  static const String _url = "ws://172.20.10.4:8000/ws/coach";
  static const int _maxReconnectAttempts = 5;
  static const Duration _heartbeatInterval = Duration(seconds: 20);

  WsConnectionState get connectionState => _state;
  Stream<Map<String, dynamic>> get commandStream => _jsonController.stream;
  Stream<Uint8List> get audioStream => _audioController.stream;
  Stream<WsConnectionState> get connectionStateStream => _stateController.stream;

  // ── 连接 ─────────────────────────────────────────────────────────────────
  Future<void> connect() async {
    if (_state == WsConnectionState.connected ||
        _state == WsConnectionState.connecting) {
      return;
    }
    _intentionalDisconnect = false;
    _setState(WsConnectionState.connecting);

    try {
      _channel = WebSocketChannel.connect(Uri.parse(_url));
      _channel!.stream.listen(
        _onMessage,
        onError: (error) => _onLostConnection('error: $error'),
        onDone: () => _onLostConnection('server closed'),
      );
      _setState(WsConnectionState.connected);
      _reconnectAttempts = 0;
      _startHeartbeat();
    } catch (e) {
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
      _setState(WsConnectionState.disconnected);
      return;
    }
    _setState(WsConnectionState.reconnecting);
    // 指数退避：1s → 2s → 4s → 8s → 16s
    final delaySeconds = 1 << _reconnectAttempts;
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
