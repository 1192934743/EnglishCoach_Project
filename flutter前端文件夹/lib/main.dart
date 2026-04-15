import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'coach_main_screen.dart';

void main() {
  runApp(const ProviderScope(child: EnglishCoachApp()));
}

class EnglishCoachApp extends ConsumerWidget {
  const EnglishCoachApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return const MaterialApp(
      debugShowCheckedModeBanner: false,
      home: CoachMainScreen(),
    );
  }
}
