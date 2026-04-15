// This is a basic Flutter widget test.
//
// To perform an interaction with a widget in your test, use the WidgetTester
// utility in the flutter_test package. For example, you can send tap and scroll
// gestures. You can also use WidgetTester to find child widgets in the widget
// tree, read text, and verify that the values of widget properties are correct.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:english_ai_app/main.dart';

void main() {
  testWidgets('App launches into coach main screen', (WidgetTester tester) async {
    await tester.pumpWidget(const EnglishCoachApp());

    expect(find.text('AI 私教'), findsOneWidget);
    expect(find.byIcon(Icons.mic), findsOneWidget);
  });
}
