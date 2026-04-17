// lib/features/onboarding/onboarding_screen.dart
//
// 3-step onboarding flow shown on first launch.
// Uses card-based selection (no AI conversation needed — fast and clear).
//
// Step 1: Learning goal       → maps to depthPreference
// Step 2: Current level       → stored as initial level
// Step 3: Practice preference → maps to newTopicAppetite
//
// Results are saved via shared_preferences + synced to backend.

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../../core/providers/settings_provider.dart';

const _kBlue   = Color(0xFF2196F3);
const _kPurple = Color(0xFF7C3AED);
const _kTeal   = Color(0xFF0D9488);

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

  // Goal → depth_preference mapping
  static const _goals = [
    _OnboardingOption(
      emoji: '💬',
      title: 'Daily Conversation',
      subtitle: 'Casual chats, travel, everyday life',
      color: _kBlue,
    ),
    _OnboardingOption(
      emoji: '✈️',
      title: 'Travel & Practical',
      subtitle: 'Airports, hotels, shopping, restaurants',
      color: Color(0xFF0891B2),
    ),
    _OnboardingOption(
      emoji: '💼',
      title: 'Work & Business',
      subtitle: 'Meetings, emails, professional settings',
      color: _kPurple,
    ),
    _OnboardingOption(
      emoji: '🎓',
      title: 'Exam Preparation',
      subtitle: 'IELTS, TOEFL, academic English',
      color: Color(0xFFD97706),
    ),
  ];

  static const _levels = [
    _OnboardingOption(
      emoji: '🌱',
      title: 'Just Starting',
      subtitle: 'I know very little English',
      color: Color(0xFF16A34A),
    ),
    _OnboardingOption(
      emoji: '📚',
      title: 'Some Knowledge',
      subtitle: 'I know basics but struggle with conversations',
      color: _kBlue,
    ),
    _OnboardingOption(
      emoji: '🗣️',
      title: 'Can Communicate',
      subtitle: 'I can have simple conversations',
      color: _kTeal,
    ),
    _OnboardingOption(
      emoji: '🚀',
      title: 'Fairly Fluent',
      subtitle: 'I want to refine my fluency and accuracy',
      color: _kPurple,
    ),
  ];

  static const _preferences = [
    _OnboardingOption(
      emoji: '🔄',
      title: 'Review & Master',
      subtitle: 'Spend more time on each topic until fluent',
      color: _kTeal,
    ),
    _OnboardingOption(
      emoji: '⚖️',
      title: 'Balanced Mix',
      subtitle: 'Mix of review and new topics',
      color: _kBlue,
    ),
    _OnboardingOption(
      emoji: '🗺️',
      title: 'Explore & Learn',
      subtitle: 'Try many different topics and scenarios',
      color: _kPurple,
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

    // Mark onboarding done
    final prefs = await SharedPreferences.getInstance();
    await prefs.setBool('onboarding_done', true);

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
      backgroundColor: const Color(0xFFF8F9FA),
      body: SafeArea(
        child: Column(
          children: [
            // ── Progress indicator ────────────────────────────────────────
            Padding(
              padding: const EdgeInsets.fromLTRB(24, 20, 24, 0),
              child: Row(
                children: List.generate(3, (i) => Expanded(
                  child: Container(
                    height: 4,
                    margin: const EdgeInsets.symmetric(horizontal: 3),
                    decoration: BoxDecoration(
                      borderRadius: BorderRadius.circular(2),
                      color: i <= _currentPage
                          ? _kBlue
                          : Colors.grey.shade200,
                    ),
                  ),
                )),
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
                  label: const Text('Back'),
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
                    title: "What's your main\nlearning goal?",
                    options: _goals,
                    selectedIndex: _goalIndex,
                    onSelected: (i) => setState(() => _goalIndex = i),
                  ),
                  _OnboardingPage(
                    title: "How would you rate\nyour current English?",
                    options: _levels,
                    selectedIndex: _levelIndex,
                    onSelected: (i) => setState(() => _levelIndex = i),
                  ),
                  _OnboardingPage(
                    title: "How do you prefer\nto practice?",
                    options: _preferences,
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
                    child: SizedBox(
                      width: double.infinity,
                      height: 54,
                      child: ElevatedButton(
                        onPressed: _canAdvance ? _next : null,
                        style: ElevatedButton.styleFrom(
                          backgroundColor: _kBlue,
                          disabledBackgroundColor: _kBlue,
                          shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(16),
                          ),
                          elevation: 0,
                        ),
                        child: Text(
                          _currentPage == 2 ? 'Start Practicing!' : 'Continue',
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
                        'Skip for now',
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
              color: Colors.black87,
            ),
          ),
          const SizedBox(height: 24),
          Expanded(
            child: ListView.separated(
              itemCount: options.length,
              separatorBuilder: (_, __) => const SizedBox(height: 12),
              itemBuilder: (context, i) {
                final opt = options[i];
                final selected = selectedIndex == i;
                return GestureDetector(
                  onTap: () => onSelected(i),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 200),
                    padding: const EdgeInsets.all(18),
                    decoration: BoxDecoration(
                      color: selected ? opt.color.withValues(alpha: 0.08) : Colors.white,
                      borderRadius: BorderRadius.circular(16),
                      border: Border.all(
                        color: selected ? opt.color : Colors.grey.shade200,
                        width: selected ? 2 : 1,
                      ),
                      boxShadow: selected ? [
                        BoxShadow(
                          color: opt.color.withValues(alpha: 0.15),
                          blurRadius: 12,
                          offset: const Offset(0, 4),
                        ),
                      ] : null,
                    ),
                    child: Row(
                      children: [
                        Text(opt.emoji, style: const TextStyle(fontSize: 28)),
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
                                  color: selected ? opt.color : Colors.black87,
                                ),
                              ),
                              const SizedBox(height: 3),
                              Text(
                                opt.subtitle,
                                style: TextStyle(fontSize: 12, color: Colors.grey[600]),
                              ),
                            ],
                          ),
                        ),
                        if (selected)
                          Icon(Icons.check_circle_rounded, color: opt.color, size: 22),
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
  final String emoji;
  final String title;
  final String subtitle;
  final Color color;

  const _OnboardingOption({
    required this.emoji,
    required this.title,
    required this.subtitle,
    required this.color,
  });
}
