import 'package:flutter_riverpod/flutter_riverpod.dart';

class SettingsState {
  final bool isChinese;
  final bool autoMode;
  final double fontSize;
  final bool showCorrection;
  final bool showTranslation;
  final bool showHints;
  final int vadTimeout;
  final bool showProgressBar; // 🌟 新增：进度条开关

  SettingsState({
    required this.isChinese,
    required this.autoMode,
    required this.fontSize,
    required this.showCorrection,
    required this.showTranslation,
    required this.showHints,
    required this.vadTimeout,
    required this.showProgressBar,
  });

  SettingsState copyWith({
    bool? isChinese,
    bool? autoMode,
    double? fontSize,
    bool? showCorrection,
    bool? showTranslation,
    bool? showHints,
    int? vadTimeout,
    bool? showProgressBar,
  }) {
    return SettingsState(
      isChinese: isChinese ?? this.isChinese,
      autoMode: autoMode ?? this.autoMode,
      fontSize: fontSize ?? this.fontSize,
      showCorrection: showCorrection ?? this.showCorrection,
      showTranslation: showTranslation ?? this.showTranslation,
      showHints: showHints ?? this.showHints,
      vadTimeout: vadTimeout ?? this.vadTimeout,
      showProgressBar: showProgressBar ?? this.showProgressBar,
    );
  }
}

class SettingsNotifier extends Notifier<SettingsState> {
  @override
  SettingsState build() {
    return SettingsState(
      isChinese: true,
      autoMode: true,
      fontSize: 12.0,
      showCorrection: false,
      showTranslation: false,
      showHints: true,
      vadTimeout: 800,
      showProgressBar: true, // 🌟 默认开启进度条
    );
  }

  void toggleLanguage(bool val) => state = state.copyWith(isChinese: val);
  void toggleAutoMode(bool val) => state = state.copyWith(autoMode: val);
  void setFontSize(double val) => state = state.copyWith(fontSize: val);
  void toggleCorrection(bool val) =>
      state = state.copyWith(showCorrection: val);
  void toggleTranslation(bool val) =>
      state = state.copyWith(showTranslation: val);
  void toggleHints(bool val) => state = state.copyWith(showHints: val);
  void setVadTimeout(int val) => state = state.copyWith(vadTimeout: val);
  // 🌟 新增 Toggle 函数
  void toggleProgressBar(bool val) =>
      state = state.copyWith(showProgressBar: val);
}

final settingsProvider = NotifierProvider<SettingsNotifier, SettingsState>(
  () => SettingsNotifier(),
);

String tr(WidgetRef ref, String en, String cn) {
  return ref.watch(settingsProvider).isChinese ? cn : en;
}
