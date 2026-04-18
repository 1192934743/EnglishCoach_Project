// lib/core/network/api_client.dart
//
// Simple HTTP client for REST endpoints.
// Shares the same base host as the WebSocket connection.

import 'dart:convert';
import 'package:http/http.dart' as http;

import 'backend_config.dart';

class ApiClient {
  static String get _baseUrl => kBackendHttpBase;

  /// 调试：当前请求所用的根地址（与 WebSocket 同源配置）。
  static String get resolvedBaseUrl => kBackendHttpBase;

  static Future<Map<String, dynamic>> getTopics({String? userId}) async {
    final uri = Uri.parse('$_baseUrl/api/topics').replace(
      queryParameters: userId != null ? {'user_id': userId} : null,
    );
    final response = await http.get(uri).timeout(const Duration(seconds: 10));
    if (response.statusCode == 200) {
      return jsonDecode(response.body) as Map<String, dynamic>;
    }
    throw Exception('Failed to load topics: ${response.statusCode}');
  }

  static Future<Map<String, dynamic>> getStats({required String userId}) async {
    final uri = Uri.parse('$_baseUrl/api/stats').replace(
      queryParameters: {'user_id': userId},
    );
    final response = await http.get(uri).timeout(const Duration(seconds: 10));
    if (response.statusCode == 200) {
      return jsonDecode(response.body) as Map<String, dynamic>;
    }
    throw Exception('Failed to load stats: ${response.statusCode}');
  }
}
