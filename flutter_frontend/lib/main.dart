import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/logging/app_logger.dart';
import 'core/network/backend_config.dart';
import 'core/providers/settings_provider.dart';
import 'features/chat/presentation/chat_screen.dart';
import 'features/chat/providers/chat_provider.dart';
import 'features/settings/screens/settings_screen.dart';
import 'features/topics/topic_browser_screen.dart';
import 'features/stats/stats_screen.dart';
import 'features/onboarding/onboarding_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // 初始化日志系统
  await AppLogger.instance.init();
  AppLogger.instance.info('App started');

  // 初始化服务器配置（加载持久化的预设）
  await initServerConfig();

  runApp(const ProviderScope(child: EnglishCoachApp()));
}

class EnglishCoachApp extends ConsumerWidget {
  const EnglishCoachApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return MaterialApp(
      debugShowCheckedModeBanner: false,
      theme: ThemeData(useMaterial3: true, colorSchemeSeed: Colors.blue),
      home: const _AppEntry(),
    );
  }
}

// ── Entry: check onboarding status ───────────────────────────────────────
class _AppEntry extends ConsumerStatefulWidget {
  const _AppEntry();
  @override
  ConsumerState<_AppEntry> createState() => _AppEntryState();
}

class _AppEntryState extends ConsumerState<_AppEntry> {
  bool? _onboardingDone;

  @override
  void initState() {
    super.initState();
    _checkOnboarding();
  }

  Future<void> _checkOnboarding() async {
    final prefs = await SharedPreferences.getInstance();
    final storedChinese = prefs.getBool('ui_is_chinese');
    if (storedChinese != null) {
      ref.read(settingsProvider.notifier).toggleLanguage(storedChinese);
    }
    final done = prefs.getBool('onboarding_done') ?? false;
    final savedLevel = prefs.getString('learner_level');
    if (savedLevel != null && savedLevel.isNotEmpty) {
      ref.read(settingsProvider.notifier).setLearnerLevel(savedLevel);
    }
    if (mounted) setState(() => _onboardingDone = done);
  }

  @override
  Widget build(BuildContext context) {
    if (_onboardingDone == null) {
      return const Scaffold(
        backgroundColor: Color(0xFFF4F6F9),
        body: Center(child: CircularProgressIndicator()),
      );
    }
    if (!_onboardingDone!) {
      return OnboardingScreen(
        onComplete: () => setState(() => _onboardingDone = true),
      );
    }
    return const MainScreen();
  }
}

// ── Main screen with 4 tabs ───────────────────────────────────────────────
class MainScreen extends ConsumerStatefulWidget {
  const MainScreen({super.key});
  @override
  ConsumerState<MainScreen> createState() => _MainScreenState();
}

class _MainScreenState extends ConsumerState<MainScreen> {
  int _currentIndex = 0;

  @override
  void initState() {
    super.initState();
    // Listen for topic_changed events and show toast
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _listenTopicChanged();
    });
  }

  void _listenTopicChanged() {
    ref.listenManual<ChatState>(chatProvider, (prev, next) {
      // Show toast when topic title changes
      if (prev != null &&
          next.currentTopicTitle != 'Simulation Practice' &&
          (prev.currentTopicTitle != next.currentTopicTitle ||
              prev.currentTopicTitleZh != next.currentTopicTitleZh)) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Row(
              children: [
                const Icon(Icons.check_circle_rounded, color: Colors.white, size: 18),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '${tr(ref, 'Now practicing:', '正在练习：')}'
                    '${chatTopicDisplayTitle(ref, next.currentTopicTitle, titleZh: next.currentTopicTitleZh.isEmpty ? null : next.currentTopicTitleZh)}',
                    style: const TextStyle(fontWeight: FontWeight.w500),
                  ),
                ),
              ],
            ),
            backgroundColor: Colors.blueAccent,
            behavior: SnackBarBehavior.floating,
            duration: const Duration(seconds: 3),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          ),
        );
        // Also switch to chat tab so user sees the new session
        setState(() => _currentIndex = 0);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _currentIndex,
        children: const [
          ChatScreen(),
          TopicBrowserScreen(),
          StatsScreen(),
          SettingsScreen(),
        ],
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: _currentIndex,
        onTap: (i) => setState(() => _currentIndex = i),
        selectedItemColor: Colors.blueAccent,
        unselectedItemColor: Colors.grey,
        type: BottomNavigationBarType.fixed,
        items: [
          BottomNavigationBarItem(
            icon: const Icon(Icons.chat_bubble_outline),
            activeIcon: const Icon(Icons.chat_bubble),
            label: tr(ref, 'Chat', '对练'),
          ),
          BottomNavigationBarItem(
            icon: const Icon(Icons.explore_outlined),
            activeIcon: const Icon(Icons.explore),
            label: tr(ref, 'Topics', '话题'),
          ),
          BottomNavigationBarItem(
            icon: const Icon(Icons.bar_chart_outlined),
            activeIcon: const Icon(Icons.bar_chart),
            label: tr(ref, 'Progress', '进度'),
          ),
          BottomNavigationBarItem(
            icon: const Icon(Icons.settings_outlined),
            activeIcon: const Icon(Icons.settings),
            label: tr(ref, 'Settings', '设置'),
          ),
        ],
      ),
    );
  }
}
