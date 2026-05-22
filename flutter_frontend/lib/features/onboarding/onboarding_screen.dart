// lib/features/onboarding/onboarding_screen.dart
//
// 3-step onboarding flow shown on first launch.
// Uses card-based selection (no AI conversation needed — fast and clear).
//
// Step 1: Learning goal       → maps to depthPreference
// Step 2: Current level       → maps to learnerLevel (NEW)
// Step 3: Practice preference → maps to newTopicAppetite
//
// Results are saved via shared_preferences + synced to backend.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../core/providers/settings_provider.dart';
import '../../core/theme/app_colors.dart';

// Onboarding color palette - using AppColors for consistency
class _OnboardingColors {
  static const Color blue = AppColors.onboardingDaily;
  static const Color teal = AppColors.onboardingTravel;
  static const Color purple = AppColors.onboardingBusiness;
  static const Color orange = AppColors.onboardingExam;
  static const Color green = AppColors.success;
}

class OnboardingScreen extends ConsumerStatefulWidget {
  final VoidCallback onComplete;
  const OnboardingScreen({super.key, required this.onComplete});

  @override
  ConsumerState<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends ConsumerState<OnboardingScreen> {
  final _pageController = PageController();
  int _currentPage = 0;

  // Selections
  int? _goalIndex;
  int? _levelIndex;
  int? _preferenceIndex;

  // Goal → depth_preference mapping（使用 Material Icons）
  List<_OnboardingOption> _goals(WidgetRef ref) => [
        _OnboardingOption(
          icon: Icons.chat_bubble_outline_rounded,
          title: tr(ref, 'Daily Conversation', '日常对话'),
          subtitle: tr(ref, 'Casual chats, travel, everyday life', '闲聊、出行、日常生活'),
          color: _OnboardingColors.blue,
        ),
        _OnboardingOption(
          icon: Icons.flight_takeoff_rounded,
          title: tr(ref, 'Travel & Practical', '旅行与实用'),
          subtitle: tr(ref, 'Airports, hotels, shopping, restaurants', '机场、酒店、购物、餐厅等场景'),
          color: _OnboardingColors.teal,
        ),
        _OnboardingOption(
          icon: Icons.work_outline_rounded,
          title: tr(ref, 'Work & Business', '职场与商务'),
          subtitle: tr(ref, 'Meetings, emails, professional settings', '会议、邮件、职场沟通'),
          color: _OnboardingColors.purple,
        ),
        _OnboardingOption(
          icon: Icons.school_outlined,
          title: tr(ref, 'Exam Preparation', '考试备考'),
          subtitle: tr(ref, 'IELTS, TOEFL, academic English', '雅思、托福、学术英语等'),
          color: _OnboardingColors.orange,
        ),
      ];

  List<_OnboardingOption> _levels(WidgetRef ref) => [
        _OnboardingOption(
          icon: Icons.eco_outlined,
          title: tr(ref, 'Just Starting', '零基础起步'),
          subtitle: tr(ref, 'I know very little English', '几乎不会说英语'),
          color: _OnboardingColors.green,
        ),
        _OnboardingOption(
          icon: Icons.menu_book_outlined,
          title: tr(ref, 'Some Knowledge', '有一点基础'),
          subtitle: tr(
            ref,
            'I know basics but struggle with conversations',
            '懂一点单词语法，对话还不流利',
          ),
          color: _OnboardingColors.blue,
        ),
        _OnboardingOption(
          icon: Icons.record_voice_over_outlined,
          title: tr(ref, 'Can Communicate', '能简单交流'),
          subtitle: tr(ref, 'I can have simple conversations', '能进行简单日常对话'),
          color: _OnboardingColors.teal,
        ),
        _OnboardingOption(
          icon: Icons.rocket_launch_outlined,
          title: tr(ref, 'Fairly Fluent', '比较流利'),
          subtitle: tr(
            ref,
            'I want to refine my fluency and accuracy',
            '希望进一步提升流利度与准确度',
          ),
          color: _OnboardingColors.purple,
        ),
      ];

  List<_OnboardingOption> _preferences(WidgetRef ref) => [
        _OnboardingOption(
          icon: Icons.replay_rounded,
          title: tr(ref, 'Review & Master', '稳扎稳打'),
          subtitle: tr(
            ref,
            'Spend more time on each topic until fluent',
            '每个话题多练几遍，练熟再换',
          ),
          color: _OnboardingColors.teal,
        ),
        _OnboardingOption(
          icon: Icons.balance_rounded,
          title: tr(ref, 'Balanced Mix', '均衡搭配'),
          subtitle: tr(ref, 'Mix of review and new topics', '复习与新话题兼顾'),
          color: _OnboardingColors.blue,
        ),
        _OnboardingOption(
          icon: Icons.explore_rounded,
          title: tr(ref, 'Explore & Learn', '广泛探索'),
          subtitle: tr(
            ref,
            'Try many different topics and scenarios',
            '多尝试不同话题与场景',
          ),
          color: _OnboardingColors.purple,
        ),
      ];

  bool get _canAdvance {
    return switch (_currentPage) {
      0 => _goalIndex != null,
      1 => _levelIndex != null,
      2 => _preferenceIndex != null,
      _ => false,
    };
  }

  void _next() {
    if (_currentPage < 2) {
      _pageController.nextPage(
        duration: const Duration(milliseconds: 350),
        curve: Curves.easeInOut,
      );
    } else {
      _finish();
    }
  }

  Future<void> _finish() async {
    // Map selections to LMS parameters
    final depthPref = switch (_goalIndex!) {
      0 => 1.5, // Daily conversation
      1 => 2.0, // Travel
      2 => 3.0, // Business
      3 => 4.0, // Exam
      _ => 1.5,
    };

    // 🌟 修复断层：英语等级映射
    final levelStr = switch (_levelIndex!) {
      0 => "Beginner",
      1 => "Elementary",
      2 => "Intermediate",
      3 => "Advanced",
      _ => "Intermediate",
    };

    final appetite = switch (_preferenceIndex!) {
      0 => 0.1, // Review-focused
      1 => 0.3, // Balanced
      2 => 0.6, // Explore
      _ => 0.3,
    };

    // Update settings provider — the ChatScreen will sync to backend on first connect
    final settingsNotifier = ref.read(settingsProvider.notifier);
    settingsNotifier.setDepthPreference(depthPref);
    settingsNotifier.setNewTopicAppetite(appetite);
    settingsNotifier.setLearnerLevel(levelStr); // 🌟 存入 Provider

    // Mark onboarding done
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('onboarding_done', true);
    await prefs.setString('learner_level', levelStr);

    widget.onComplete();
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.background,
      body: SafeArea(
        child: Column(
          children: [
            // ── Progress indicator ────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 20, 24, 0),
              child: Row(
                children: List.generate(
                  3,
                  (i) => Expanded(
                    child: Container(
                      height: 4,
                      margin: const EdgeInsets.symmetric(horizontal: 3),
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(2),
                        color: i <= _currentPage
                            ? AppColors.primary
                            : AppColors.border,
                      ),
                    ),
                  ),
                ),
              ),
            ),
            const SizedBox(height: 8),
            if (_currentPage > 0)
              Align(
                alignment: Alignment.centerLeft,
                child: TextButton.icon(
                  onPressed: () => _pageController.previousPage(
                    duration: const Duration(milliseconds: 300),
                    curve: Curves.easeInOut,
                  ),
                  icon: const Icon(Icons.arrow_back_ios, size: 16),
                  label: Text(tr(ref, 'Back', '返回')),
                  style: TextButton.styleFrom(foregroundColor: Colors.grey),
                ),
              )
            else
              const SizedBox(height: 12),

            // ── Page content ──────────────────────────────────────────────
            Expanded(
              child: PageView(
                controller: _pageController,
                physics: const NeverScrollableScrollPhysics(),
                onPageChanged: (i) => setState(() => _currentPage = i),
                children: [
                  _OnboardingPage(
                    title: tr(ref, "What's your main\nlearning goal?", '你的主要学习\n目标是？'),
                    options: _goals(ref),
                    selectedIndex: _goalIndex,
                    onSelected: (i) => setState(() => _goalIndex = i),
                  ),
                  _OnboardingPage(
                    title: tr(
                      ref,
                      "How would you rate\nyour current English?",
                      '你目前的英语\n水平如何？',
                    ),
                    options: _levels(ref),
                    selectedIndex: _levelIndex,
                    onSelected: (i) => setState(() => _levelIndex = i),
                  ),
                  _OnboardingPage(
                    title: tr(ref, "How do you prefer\nto practice?", '你更喜欢\n哪种练习方式？'),
                    options: _preferences(ref),
                    selectedIndex: _preferenceIndex,
                    onSelected: (i) => setState(() => _preferenceIndex = i),
                  ),
                ],
              ),
            ),

            // ── CTA button ────────────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
              child: Column(
                children: [
                  AnimatedOpacity(
                    opacity: _canAdvance ? 1.0 : 0.4,
                    duration: const Duration(milliseconds: 200),
                    child:                   SizedBox(
                      width: double.infinity,
                      height: 54,
                      child: ElevatedButton(
                        onPressed: _canAdvance ? _next : null,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: AppColors.primary,
                          disabledBackgroundColor: AppColors.primary,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(16),
                          ),
                          elevation: 0,
                        ),
                        child: Text(
                          _currentPage == 2
                              ? tr(ref, 'Start Practicing!', '开始练习！')
                              : tr(ref, 'Continue', '继续'),
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.bold,
                          ),
                        ),
                      ),
                    ),
                  ),
                  if (_currentPage == 0) ...[
                    const SizedBox(height: 12),
                    TextButton(
                      onPressed: () async {
                        final prefs = await SharedPreferences.getInstance();
                        await prefs.setBool('onboarding_done', true);
                        widget.onComplete();
                      },
                      child: Text(
                        tr(ref, 'Skip for now', '暂时跳过'),
                        style: TextStyle(color: Colors.grey[500], fontSize: 13),
                      ),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// ─────────────────────────────────────────────────────────────────────────────

class _OnboardingPage extends StatelessWidget {
  final String title;
  final List<_OnboardingOption> options;
  final int? selectedIndex;
  final ValueChanged<int> onSelected;

  const _OnboardingPage({
    required this.title,
    required this.options,
    required this.selectedIndex,
    required this.onSelected,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const SizedBox(height: 8),
          Text(
            title,
            style: const TextStyle(
              fontSize: 26,
              fontWeight: FontWeight.bold,
              height: 1.25,
              color: AppColors.textPrimary,
            ),
          ),
          const SizedBox(height: 24),
          Expanded(
            child: ListView.separated(
              itemCount: options.length,
              separatorBuilder: (_, _) => const SizedBox(height: 12),
              itemBuilder: (context, i) {
                final opt = options[i];
                final selected = selectedIndex == i;
                return GestureDetector(
                  onTap: () => onSelected(i),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    padding: const EdgeInsets.all(18),
                    decoration: BoxDecoration(
                      color: selected
                          ? opt.color.withValues(alpha: 0.08)
                          : Colors.white,
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(
                        color: selected ? opt.color : Colors.grey.shade200,
                        width: selected ? 2 : 1,
                      ),
                      boxShadow: selected
                          ? [
                              BoxShadow(
                                color: opt.color.withValues(alpha: 0.15),
                                blurRadius: 12,
                                offset: const Offset(0, 4),
                              ),
                            ]
                          : null,
                    ),
                      child: Row(
                      children: [
                        Container(
                          padding: const EdgeInsets.all(10),
                          decoration: BoxDecoration(
                            color: opt.color.withValues(alpha: 0.1),
                            borderRadius: BorderRadius.circular(12),
                          ),
                          child: Icon(opt.icon, color: opt.color, size: 24),
                        ),
                        const SizedBox(width: 14),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                opt.title,
                                style: TextStyle(
                                  fontSize: 15,
                                  fontWeight: FontWeight.bold,
                                  color: selected ? opt.color : AppColors.textPrimary,
                                ),
                              ),
                              const SizedBox(height: 3),
                              Text(
                                opt.subtitle,
                                style: TextStyle(
                                  fontSize: 12,
                                  color: AppColors.textSecondary,
                                ),
                              ),
                            ],
                          ),
                        ),
                        if (selected)
                          Icon(
                            Icons.check_circle_rounded,
                            color: opt.color,
                            size: 22,
                          ),
                      ],
                    ),
                  ),
                );
              },
            ),
          ),
        ],
      ),
    );
  }
}

class _OnboardingOption {
  final IconData icon;
  final String title;
  final String subtitle;
  final Color color;

  const _OnboardingOption({
    required this.icon,
    required this.title,
    required this.subtitle,
    required this.color,
  });
}
