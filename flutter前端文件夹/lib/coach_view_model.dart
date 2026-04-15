import 'dart:convert';
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

class CoachViewModel extends ChangeNotifier {
  WebSocketChannel? _channel;

  // --- 状态数据 ---
  double topicProgress = 0.0; // 0.0 到 1.0
  Map<String, dynamic>? currentTeachingData;
  bool isConnected = false;

  // 初始化 WebSocket 连接
  void connect(String wsUrl) {
    try {
      _channel = WebSocketChannel.connect(Uri.parse(wsUrl));
      isConnected = true;
      notifyListeners();

      _channel!.stream.listen(
        (message) {
          if (message is String) {
            _handleJsonProtocol(message);
          } else if (message is Uint8List) {
            _handleAudioStream(message);
          }
        },
        onDone: () {
          isConnected = false;
          notifyListeners();
          debugPrint("WebSocket 连接已关闭");
        },
        onError: (error) {
          isConnected = false;
          notifyListeners();
          debugPrint("WebSocket 发生错误: $error");
        },
      );
    } catch (e) {
      debugPrint("WebSocket 连接初始化失败: $e");
    }
  }

  // 解析并分发 JSON 通信契约
  void _handleJsonProtocol(String message) {
    try {
      final data = jsonDecode(message);
      final event = data['event'];

      switch (event) {
        case 'teaching_data':
          currentTeachingData = data['data'];
          notifyListeners();
          break;
        case 'topic_mastery_reached':
          double rawProgress = (data['progress'] as num).toDouble();
          topicProgress = (rawProgress / 100.0).clamp(0.0, 1.0);
          notifyListeners();
          break;
        case 'tts_finished':
        case 'role_swapped':
          debugPrint("接收到状态流转信号: $event");
          break;
        default:
          debugPrint("未知的 JSON 事件: $event");
      }
    } catch (e) {
      debugPrint("解析 JSON 协议失败: $e - 数据: $message");
    }
  }

  // 处理二进制 TTS 流
  void _handleAudioStream(Uint8List audioBytes) {
    debugPrint("接收到二进制音频流，长度: ${audioBytes.length}");
  }

  // 发送音频流给后端
  void sendPcmData(Uint8List pcmData) {
    if (_channel != null && isConnected) {
      _channel!.sink.add(pcmData);
    }
  }

  // 清除当前的教学面板
  void clearTeachingData() {
    currentTeachingData = null;
    notifyListeners();
  }

  @override
  void dispose() {
    _channel?.sink.close();
    super.dispose();
  }
}
