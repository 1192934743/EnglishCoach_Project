import 'package:flutter/material.dart';

/// EnglishCoach 配色系统 - 深色模式
///
/// 设计原则：
/// - 使用更亮的配色提升对比度
/// - 背景层级提升，避免过于昏暗
/// - 文字色使用接近白色的值确保可读性
class AppColorsDark {
  AppColorsDark._();

  // ── 主色调 - 亮色范围 ─────────────────────────────────────────
  static const Color primary = Color(0xFF60A5FA);
  static const Color primaryLight = Color(0xFF93C5FD);
  static const Color primaryDark = Color(0xFF3B82F6);

  // ── 强调色 ─────────────────────────────────────────────────
  static const Color accent = Color(0xFFFF6B6B);
  static const Color accentLight = Color(0xFFFF8E8E);

  // ── 状态色 ─────────────────────────────────────────────────
  static const Color success = Color(0xFF4ADE80);
  static const Color successLight = Color(0xFF86EFAC);
  static const Color warning = Color(0xFFFBBF24);
  static const Color warningLight = Color(0xFFFDE047);
  static const Color error = Color(0xFFF87171);
  static const Color errorLight = Color(0xFFFCA5A5);

  // ── 背景色系 - 更亮的背景提升对比度 ───────────────────────────
  static const Color background = Color(0xFF1E293B);
  static const Color surface = Color(0xFF334155);
  static const Color surfaceVariant = Color(0xFF475569);

  // ── 文字色系 ────────────────────────────────────────────────
  static const Color textPrimary = Color(0xFFF8FAFC);   // 接近白色
  static const Color textSecondary = Color(0xFFCBD5E1);   // 提升对比度
  static const Color textTertiary = Color(0xFF94A3B8);

  // ── 边框与分割线 ────────────────────────────────────────────
  static const Color border = Color(0xFF475569);
  static const Color divider = Color(0xFF334155);

  // ── 渐变色 ─────────────────────────────────────────────────
  static const LinearGradient primaryGradient = LinearGradient(
    colors: [Color(0xFF60A5FA), Color(0xFFA78BFA)],
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
  );

  static const LinearGradient successGradient = LinearGradient(
    colors: [Color(0xFF4ADE80), Color(0xFF34D399)],
    begin: Alignment.topLeft,
    end: Alignment.bottomRight,
  );

  // ── 用户气泡色 ─────────────────────────────────────────────
  static const Color userBubbleBg = Color(0xFF1E3A5F);  // 深蓝背景
  static const Color userBubbleBorder = Color(0xFF3B82F6);  // 亮蓝边框

  // ── AI 气泡色 ──────────────────────────────────────────────
  static const Color aiBubbleBg = Color(0xFF334155);  // 深灰白
  static const Color aiBubbleBorder = Color(0xFF475569);  // 中灰边框

  // ── 教辅数据色 ─────────────────────────────────────────────
  static const Color translationBg = Color(0xFF1E3A5F);  // 深蓝
  static const Color hintBg = Color(0xFF14532D);  // 深绿
  static const Color correctionBg = Color(0xFF7C2D12);  // 深橙

  // ── Onboarding 目标色 ────────────────────────────────────────
  static const Color onboardingDaily = Color(0xFF60A5FA);
  static const Color onboardingTravel = Color(0xFF22D3EE);
  static const Color onboardingBusiness = Color(0xFFA78BFA);
  static const Color onboardingExam = Color(0xFFFBBF24);
  static const Color onboardingDailyLight = Color(0xFF1E3A5F);
  static const Color onboardingTravelLight = Color(0xFF164E63);
  static const Color onboardingBusinessLight = Color(0xFF4C1D95);
  static const Color onboardingExamLight = Color(0xFF78350F);
}
