import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'core/logging/app_logger.dart';
import 'core/network/backend_config.dart';
import 'core/providers/settings_provider.dart';  // settingsProvider, tr, mainTabIndexProvider, etc.
import 'core/theme/app_theme.dart';  // New theme system
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
      title: 'EnglishCoach',
      theme: AppTheme.light,    // 使用新的浅色主题
      darkTheme: AppTheme.dark,  // 使用新的深色主题
      themeMode: ThemeMode.system,  // 跟随系统设置
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
  Timer? _aiFirstStrikeTimer;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _listenTopicChanged();
    });
  }

  void _listenTopicChanged() {
    ref.listenManual<ChatState>(chatProvider, (prev, next) {
      // 检测话题切换成功
      if (prev != null && next.currentTopicTitle != prev.currentTopicTitle) {
        // 显示 toast
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
            backgroundColor: const Color(0xFF1A1A2E),
            behavior: SnackBarBehavior.floating,
            duration: const Duration(seconds: 3),
            shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
          ),
        );

        // 切换到对话 Tab
        ref.read(mainTabIndexProvider.notifier).setTab(0);

        // 取消之前的定时器，防止多次触发
        _aiFirstStrikeTimer?.cancel();
        // 使用 Timer 替代 Future.delayed，可在 dispose 中取消
        _aiFirstStrikeTimer = Timer(const Duration(milliseconds: 500), () {
          if (mounted) {
            ref.read(chatProvider.notifier).triggerAiFirstStrike();
          }
        });

        ref.read(chatProvider.notifier).onTopicChanged();
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    final tabIndex = ref.watch(mainTabIndexProvider);

    return Scaffold(
      body: IndexedStack(
        index: tabIndex,
        children: const [
          ChatScreen(),
          TopicBrowserScreen(),
          StatsScreen(),
          SettingsScreen(),
        ],
      ),
      bottomNavigationBar: BottomNavigationBar(
        currentIndex: tabIndex,
        onTap: (i) => ref.read(mainTabIndexProvider.notifier).setTab(i),
        type: BottomNavigationBarType.fixed,
        items: const [
          BottomNavigationBarItem(
            icon: Icon(Icons.chat_bubble_outline_rounded),
            activeIcon: Icon(Icons.chat_bubble_rounded),
            label: '对练',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.explore_outlined),
            activeIcon: Icon(Icons.explore),
            label: '话题',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.bar_chart_outlined),
            activeIcon: Icon(Icons.bar_chart),
            label: '进度',
          ),
          BottomNavigationBarItem(
            icon: Icon(Icons.settings_outlined),
            activeIcon: Icon(Icons.settings),
            label: '设置',
          ),
        ],
      ),
    );
  }

  @override
  void dispose() {
    _aiFirstStrikeTimer?.cancel();
    super.dispose();
  }
}
