// lib/core/network/api_client.dart
//
// Simple HTTP client for REST endpoints.
// Shares the same base host as the WebSocket connection.

import 'dart:convert';
import 'package:http/http.dart' as http;

class ApiClient {
  // Keep in sync with WebSocketClient._url
  static const String _baseUrl = "http://172.20.10.4:8000";

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
