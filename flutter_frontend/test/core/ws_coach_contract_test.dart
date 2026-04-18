import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';

/// 与 `python_backend/server.py` 中 JSON 指令字段对齐的契约单测（不连网）。
void main() {
  group('Coach WebSocket JSON contract', () {
    test('user_finish_speaking includes trace_id and wall ms', () {
      final m = <String, dynamic>{
        'action': 'user_finish_speaking',
        'user_id': 'u-test',
        'trace_id': '${'a' * 32}',
        'client_submit_wall_ms': 1700000000000,
      };
      final s = jsonEncode(m);
      final round = jsonDecode(s) as Map<String, dynamic>;
      expect(round['action'], 'user_finish_speaking');
      expect(round['trace_id'], isA<String>());
      expect((round['trace_id'] as String).length, 32);
      expect(round['client_submit_wall_ms'], isA<int>());
    });

    test('client_latency_report shape', () {
      final m = <String, dynamic>{
        'action': 'client_latency_report',
        'trace_id': 'abcd' * 4,
        'submit_to_first_pcm_ms': 2100,
        'submit_to_tts_finished_ms': 10000,
        'had_first_pcm': true,
        'client_report_wall_ms': 1700000000000,
      };
      final r = jsonDecode(jsonEncode(m)) as Map<String, dynamic>;
      expect(r['action'], 'client_latency_report');
      expect(r['had_first_pcm'], isTrue);
    });
  });
}
