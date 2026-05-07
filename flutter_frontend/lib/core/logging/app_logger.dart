import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

enum LogLevel { debug, info, warning, error }

class AppLogger {
  static final AppLogger _instance = AppLogger._internal();
  static AppLogger get instance => _instance;

  AppLogger._internal();

  File? _logFile;
  static const String _logFileName = 'app_flutter.log';
  static const int _maxFileSizeBytes = 5 * 1024 * 1024; // 5MB

  Future<void> init() async {
    final directory = await getApplicationDocumentsDirectory();
    final logDir = Directory(directory.path);
    if (!await logDir.exists()) {
      await logDir.create(recursive: true);
    }
    _logFile = File('${directory.path}/$_logFileName');
    await _log('INFO', 'Logger initialized');
  }

  Future<void> _writeLog(String level, String message) async {
    final timestamp = DateTime.now().toIso8601String();
    final logEntry = '[$timestamp] [$level] $message\n';

    if (_logFile != null) {
      try {
        // 检查文件是否存在，避免文件不存在时 length() 抛出异常
        if (await _logFile!.exists()) {
          final fileSize = await _logFile!.length();
          if (fileSize > _maxFileSizeBytes) {
            await _rotateLog();
          }
        }
        await _logFile!.writeAsString(logEntry, mode: FileMode.append);
      } catch (e) {
        debugPrint('Failed to write log: $e');
      }
    }

    debugPrint(logEntry.trim());
  }

  Future<void> _rotateLog() async {
    if (_logFile == null) return;

    final rotatedFile = File('${_logFile!.path}.old');
    if (await rotatedFile.exists()) {
      await rotatedFile.delete();
    }
    await _logFile!.rename(rotatedFile.path);
    _logFile = File(_logFile!.path);
  }

  Future<void> _log(String level, String message) async {
    await _writeLog(level, message);
  }

  void debug(String message) {
    _writeLog('DEBUG', message);
  }

  void info(String message) {
    _writeLog('INFO', message);
  }

  void warning(String message) {
    _writeLog('WARNING', message);
  }

  void error(String message) {
    _writeLog('ERROR', message);
  }

  static void log(String prefix, String message) {
    instance.info('[$prefix] $message');
  }
}
