import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:english_ai_app/main.dart';

void main() {
  testWidgets('App launches with chat tab and ProviderScope', (WidgetTester tester) async {
    await tester.pumpWidget(
      const ProviderScope(child: EnglishCoachApp()),
    );
    // Avoid pumpAndSettle: repeating animations; flush ChatNotifier's 500ms delayed warmup.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 600));

    expect(find.text('场景对练'), findsOneWidget);
    expect(find.text('Simulation Practice'), findsOneWidget);
    expect(find.byIcon(Icons.mic_rounded), findsWidgets);
  });
}
