import 'package:flutter/material.dart';

/// EnglishCoach 配色系统 - 浅色模式
///
/// 设计规范：
/// - 主色：科技蓝 #2563EB
/// - 强调色：珊瑚红 #FF6B6B
/// - 背景：柔和白 #F8FAFC
/// - 统一使用 4px 网格系统
class AppColors {
  AppColors._();

  // ── 主色调 - 科技蓝 ─────────────────────────────────────────────
  static const Color primary = Color(0xFF2563EB);
  static const Color primaryLight = Color(0xFF3B82F6);
  static const Color primaryDark = Color(0xFF1D4ED8);

  // ── 强调色 - 珊瑚红 ─────────────────────────────────────────────
  static const Color accent = Color(0xFFFF6B6B);
  static const Color accentLight = Color(0xFFFF8E8E);

  // ── 状态色 ─────────────────────────────────────────────────────
  static const Color success = Color(0xFF22C55E);
  static const Color successLight = Color(0xFF4ADE80);
  static const Color warning = Color(0xFFFBBF24);
  static const Color warningLight = Color(0xFFFCD34D);
  static const Color error = Color(0xFFEF4444);
  static const Color errorLight = Color(0xFFF87171);

  // ── 背景色系 ───────────────────────────────────────────────────
  static const Color background = Color(0xFFF8FAFC);
  static const Color surface = Color(0xFFFFFFFF);
  static const Color surfaceVariant = Color(0xFFF1F5F9);

  // ── 文字色系 ───────────────────────────────────────────────────
  static const Color textPrimary = Color(0xFF1E293B);
  static const Color textSecondary = Color(0xFF64748B);
  static const Color textTertiary = Color(0xFF94A3B8);

  // ── 边框与分割线 ────────────────────────────────────────────────
  static const Color border = Color(0xFFE2E8F0);
  static const Color divider = Color(0xFFF1F5F9);

  // ── 渐变色 ─────────────────────────────────────────────────────
  static const LinearGradient primaryGradient = LinearGradient(
    colors: [Color(0xFF2563EB), Color(0xFF7C3AED)],
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
  );

  static const LinearGradient successGradient = LinearGradient(
    colors: [Color(0xFF22C55E), Color(0xFF10B981)],
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
  );

  // ── 用户气泡色 ─────────────────────────────────────────────────
  static const Color userBubbleBg = Color(0xFFE8F0FE);  // 浅蓝背景
  static const Color userBubbleBorder = Color(0xFFBFDBFE);  // 蓝色边框

  // ── AI 气泡色 ─────────────────────────────────────────────────
  static const Color aiBubbleBg = Color(0xFFFFFFFF);  // 纯白
  static const Color aiBubbleBorder = Color(0xFFE2E8F0);  // 浅灰边框

  // ── 教辅数据色 ─────────────────────────────────────────────────
  static const Color translationBg = Color(0xFFF0F8FF);  // 极浅蓝
  static const Color hintBg = Color(0xFFF1F8E9);  // 极浅绿
  static const Color correctionBg = Color(0xFFFFF7ED);  // 极浅橙

  // ── Onboarding 目标色 ───────────────────────────────────────────
  static const Color onboardingDaily = Color(0xFF2196F3);
  static const Color onboardingTravel = Color(0xFF0891B2);
  static const Color onboardingBusiness = Color(0xFF7C3AED);
  static const Color onboardingExam = Color(0xFFD97706);
  static const Color onboardingDailyLight = Color(0xFFE3F2FD);
  static const Color onboardingTravelLight = Color(0xFFE0F2F1);
  static const Color onboardingBusinessLight = Color(0xFFEDE7F6);
  static const Color onboardingExamLight = Color(0xFFFEF3C7);
}
