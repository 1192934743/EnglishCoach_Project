import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

// 使用 Provider 管理 WebSocketClient 实例
final websocketProvider = Provider<WebSocketClient>((ref) {
  final client = WebSocketClient();
  ref.onDispose(() => client.disconnect());
  return client;
});

class WebSocketClient {
  WebSocketChannel? _channel;

  final StreamController<Map<String, dynamic>> _jsonCommandController =
      StreamController.broadcast();
  final StreamController<Uint8List> _audioStreamController =
      StreamController.broadcast();

  // 👇 就是这里！必须作为类的属性，写在所有函数的外面
  final String _url = "ws://172.20.10.4:8000/ws/coach";

  Future<void> connect() async {
    if (_channel != null) return;
    try {
      _channel = WebSocketChannel.connect(Uri.parse(_url));

      _channel!.stream.listen(
        (message) {
          if (message is String) {
            try {
              final data = jsonDecode(message);
              _jsonCommandController.add(data);
            } catch (e) {
              print("❌ JSON 解析失败: $e");
            }
          } else if (message is List<int>) {
            _audioStreamController.add(Uint8List.fromList(message));
          }
        },
        onError: (error) => print("❌ WS Error: $error"),
        onDone: () => disconnect(),
      );
      print("🌐 WebSocket Connected");
    } catch (e) {
      print("💥 WS Connect Failed: $e");
    }
  }

  void sendCommand(String action, Map<String, dynamic> data) {
    if (_channel != null) {
      final payload = jsonEncode({"action": action, ...data});
      _channel!.sink.add(payload);
    }
  }

  void sendAudio(List<int> audioData) {
    if (_channel != null) {
      _channel!.sink.add(audioData);
    }
  }

  Stream<Map<String, dynamic>> get commandStream =>
      _jsonCommandController.stream;
  Stream<Uint8List> get audioStream => _audioStreamController.stream;

  void disconnect() {
    _channel?.sink.close(1001);
    _channel = null;
    print("🔌 WebSocket Disconnected");
  }
}
