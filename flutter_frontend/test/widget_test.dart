import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:english_ai_app/main.dart';

void main() {
  testWidgets('App launches with chat tab and ProviderScope', (WidgetTester tester) async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({'onboarding_done': true});

    await tester.pumpWidget(
      const ProviderScope(child: EnglishCoachApp()),
    );
    // _AppEntry awaits SharedPreferences then builds MainScreen → ChatScreen.
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));
    await tester.pump(const Duration(milliseconds: 600));

    expect(find.byType(MaterialApp), findsOneWidget);
    // Default [SettingsNotifier] uses isChinese: true → bottom nav 中文标签
    expect(find.text('对练'), findsWidgets);
    expect(find.text('话题'), findsWidgets);
    expect(
      find.byElementPredicate(
        (el) =>
            el.widget is Icon &&
            ((el.widget as Icon).icon == Icons.chat_bubble ||
                (el.widget as Icon).icon == Icons.chat_bubble_outline),
      ),
      findsWidgets,
    );
    // ChatNotifier schedules WS warmup (500ms) + internal timers — drain before dispose.
    await tester.pump(const Duration(seconds: 2));
  });
}
