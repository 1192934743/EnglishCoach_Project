import 'package:flutter_riverpod/flutter_riverpod.dart';

class SettingsState {
  final bool isChinese;
  final bool autoMode;
  final double fontSize;
  final bool showCorrection;
  final bool showTranslation;
  final bool showHints;
  final int vadTimeout;

  SettingsState({
    required this.isChinese,
    required this.autoMode,
    required this.fontSize,
    required this.showCorrection,
    required this.showTranslation,
    required this.showHints,
    required this.vadTimeout,
  });

  SettingsState copyWith({
    bool? isChinese,
    bool? autoMode,
    double? fontSize,
    bool? showCorrection,
    bool? showTranslation,
    bool? showHints,
    int? vadTimeout,
  }) {
    return SettingsState(
      isChinese: isChinese ?? this.isChinese,
      autoMode: autoMode ?? this.autoMode,
      fontSize: fontSize ?? this.fontSize,
      showCorrection: showCorrection ?? this.showCorrection,
      showTranslation: showTranslation ?? this.showTranslation,
      showHints: showHints ?? this.showHints,
      vadTimeout: vadTimeout ?? this.vadTimeout,
    );
  }
}

class SettingsNotifier extends Notifier<SettingsState> {
  @override
  SettingsState build() {
    return SettingsState(
      isChinese: true,
      autoMode: true,
      fontSize: 12.0, // 🌟 默认字体调小
      showCorrection: false,
      showTranslation: false,
      showHints: true,
      vadTimeout: 800, // 🌟 默认 800ms
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
}

final settingsProvider = NotifierProvider<SettingsNotifier, SettingsState>(
  () => SettingsNotifier(),
);

String tr(WidgetRef ref, String en, String cn) {
  return ref.watch(settingsProvider).isChinese ? cn : en;
}
