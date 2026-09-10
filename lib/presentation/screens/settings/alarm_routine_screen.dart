import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/services/alarm_routine_service.dart';
import '../../../data/services/alarm_service.dart';
import '../../widgets/pressable_tile.dart';

/// The post-alarm routine editor: what Buddy covers right after "I'm up".
///
/// This replaced the coming-soon preview and kept its layout. The list is the
/// contract: a row's switch includes that section in the morning brief, and
/// drag order is the order Buddy speaks it. Saving PUTs the whole config;
/// nothing here touches the native alarm schedule, because the brief runs on
/// the network moment after dismissal, not on the ringing path.
class AlarmRoutineScreen extends StatefulWidget {
  const AlarmRoutineScreen({super.key, this.initialConfig});

  /// Config the alarm page already fetched, so the common path opens instantly.
  final AlarmRoutineConfig? initialConfig;

  @override
  State<AlarmRoutineScreen> createState() => _AlarmRoutineScreenState();
}

class _AlarmRoutineScreenState extends State<AlarmRoutineScreen> {
  static const _labels = <String, String>{
    'weather': 'Tell me about the weather',
    'calendar': "Tell me about today's calendar",
    'tasks': "Tell me today's tasks",
    'joke': 'Tell me a joke',
  };

  static const _icons = <String, IconData>{
    'weather': Icons.cloud_outlined,
    'calendar': Icons.calendar_today_outlined,
    'tasks': Icons.task_alt_rounded,
    'joke': Icons.sentiment_very_satisfied_outlined,
  };

  bool _loading = true;
  bool _saving = false;
  bool _enabled = false;
  bool _showWeather = false;
  List<String> _order = List.of(AlarmRoutineService.defaultActions);
  final Set<String> _included = {};

  @override
  void initState() {
    super.initState();
    final initial = widget.initialConfig;
    if (initial != null) {
      _apply(initial);
      _loading = false;
    } else {
      unawaited(_load());
    }
  }

  Future<void> _load() async {
    final config = await context.read<AlarmRoutineService>().fetchConfig();
    if (!mounted) return;
    if (config == null) {
      setState(() => _loading = false);
      _showMessage("Couldn't reach Buddy. Pull back and try again.");
      return;
    }
    setState(() {
      _apply(config);
      _loading = false;
    });
  }

  void _apply(AlarmRoutineConfig config) {
    _enabled = config.enabled;
    _showWeather = config.showWeather;
    _included
      ..clear()
      ..addAll(config.actions);
    // Configured order first, then anything not yet included, so every known
    // action always has a row and a place.
    _order = [
      ...config.actions,
      ...AlarmRoutineService.defaultActions.where(
        (t) => !config.actions.contains(t),
      ),
    ];
  }

  AlarmRoutineConfig get _config => AlarmRoutineConfig(
    enabled: _enabled,
    showWeather: _showWeather,
    actions: [
      for (final type in _order)
        if (_included.contains(type)) type,
    ],
  );

  void _toggleAction(String type, bool include) {
    HapticFeedback.selectionClick();
    setState(() {
      if (include) {
        _included.add(type);
      } else {
        _included.remove(type);
      }
    });
  }

  void _reorder(int oldIndex, int newIndex) {
    HapticFeedback.selectionClick();
    setState(() {
      final moved = _order.removeAt(oldIndex);
      _order.insert(newIndex, moved);
    });
  }

  void _recordSuggestedInterest(String feature, String label) {
    HapticFeedback.lightImpact();
    unawaited(context.read<AlarmService>().recordComingSoonInterest(feature));
    _showMessage('Thanks for the interest — $label is coming soon.');
  }

  Future<void> _save() async {
    if (_saving) return;
    if (_enabled && _config.actions.isEmpty) {
      _showMessage('Pick at least one thing for Buddy to cover.');
      return;
    }
    HapticFeedback.mediumImpact();
    setState(() => _saving = true);
    final stored = await context.read<AlarmRoutineService>().saveConfig(
      _config,
    );
    if (!mounted) return;
    setState(() => _saving = false);
    if (stored == null) {
      _showMessage("Couldn't save that. Try once more.");
      return;
    }
    Navigator.pop(context, stored);
  }

  void _showMessage(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..clearSnackBars()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(18, 10, 14, 4),
                child: Row(
                  children: [
                    GlassIconButton(
                      icon: Icons.close_rounded,
                      onTap: () => Navigator.pop(context),
                    ),
                    const Spacer(),
                    Switch.adaptive(
                      value: _enabled,
                      onChanged: _loading
                          ? null
                          : (value) {
                              HapticFeedback.selectionClick();
                              setState(() => _enabled = value);
                            },
                    ),
                  ],
                ),
              ),
              Expanded(
                child: _loading
                    ? const Center(
                        child: CircularProgressIndicator(
                          color: AppColors.alarmAccent,
                          strokeWidth: 2,
                        ),
                      )
                    : _buildEditor(context),
              ),
              if (!_loading)
                Container(
                  width: double.infinity,
                  padding: EdgeInsets.fromLTRB(
                    20,
                    12,
                    20,
                    12 + MediaQuery.viewPaddingOf(context).bottom,
                  ),
                  decoration: const BoxDecoration(
                    color: AppColors.deepBackground,
                    border: Border(top: BorderSide(color: AppColors.divider)),
                  ),
                  child: FilledButton(
                    onPressed: _saving ? null : _save,
                    style: FilledButton.styleFrom(
                      backgroundColor: AppColors.alarmAccent,
                      foregroundColor: AppColors.onAccent,
                      disabledBackgroundColor: AppColors.surfaceVariant,
                      disabledForegroundColor: AppColors.textDisabled,
                      minimumSize: const Size.fromHeight(52),
                    ),
                    child: _saving
                        ? const SizedBox(
                            width: 20,
                            height: 20,
                            child: CircularProgressIndicator(
                              color: AppColors.onAccent,
                              strokeWidth: 2,
                            ),
                          )
                        : Text(_enabled ? 'Save' : 'Save as off'),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildEditor(BuildContext context) {
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 0, end: 1),
      duration: const Duration(milliseconds: 420),
      curve: Curves.easeOutCubic,
      builder: (context, value, child) => Opacity(
        opacity: value,
        child: Transform.translate(
          offset: Offset(0, 18 * (1 - value)),
          child: child,
        ),
      ),
      child: ListView(
        padding: const EdgeInsets.fromLTRB(20, 24, 20, 28),
        children: [
          Row(
            children: [
              const Icon(
                Icons.alarm_rounded,
                color: AppColors.alarmAccent,
                size: 34,
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Text(
                  'Morning Routine',
                  style: Theme.of(context).textTheme.headlineSmall,
                ),
              ),
            ],
          ),
          const SizedBox(height: 24),
          Text(
            "When you tell Buddy you're up, this Routine will",
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 6),
          Text(
            'Drag to reorder. Buddy covers these in your chat, in this order.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
          const SizedBox(height: 18),
          AnimatedOpacity(
            opacity: _enabled ? 1 : 0.55,
            duration: const Duration(milliseconds: 220),
            child: ReorderableListView(
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              buildDefaultDragHandles: false,
              onReorderItem: _reorder,
              children: [
                for (var i = 0; i < _order.length; i++)
                  _RoutineActionRow(
                    key: ValueKey(_order[i]),
                    index: i,
                    icon: _icons[_order[i]] ?? Icons.podcasts_rounded,
                    label: _labels[_order[i]] ?? _order[i],
                    included: _included.contains(_order[i]),
                    onChanged: (value) => _toggleAction(_order[i], value),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 28),
          Text(
            'Suggested actions',
            style: Theme.of(context).textTheme.titleMedium,
          ),
          const SizedBox(height: 8),
          Text(
            'Tap one to tell Buddy you want it next.',
            style: Theme.of(context).textTheme.bodySmall,
          ),
          const SizedBox(height: 12),
          _SuggestedActionRow(
            icon: Icons.assistant_rounded,
            label: 'Tell me about my commute',
            onTap: () => _recordSuggestedInterest(
              'routine_commute',
              'Commute in Routines',
            ),
          ),
          _SuggestedActionRow(
            icon: Icons.play_circle_outline_rounded,
            label: 'Play the news',
            onTap: () =>
                _recordSuggestedInterest('routine_news', 'News in Routines'),
          ),
        ],
      ),
    );
  }
}

class _RoutineActionRow extends StatelessWidget {
  const _RoutineActionRow({
    super.key,
    required this.index,
    required this.icon,
    required this.label,
    required this.included,
    required this.onChanged,
  });

  final int index;
  final IconData icon;
  final String label;
  final bool included;
  final ValueChanged<bool> onChanged;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: FauxGlassCard(
        borderRadius: 18,
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        child: Row(
          children: [
            ReorderableDragStartListener(
              index: index,
              child: const Padding(
                padding: EdgeInsets.symmetric(vertical: 9),
                child: Icon(
                  Icons.drag_handle_rounded,
                  color: AppColors.textTertiary,
                  size: 23,
                ),
              ),
            ),
            const SizedBox(width: 16),
            Icon(icon, color: AppColors.alarmAccent, size: 25),
            const SizedBox(width: 16),
            Expanded(
              child: Text(label, style: Theme.of(context).textTheme.titleMedium),
            ),
            Switch.adaptive(value: included, onChanged: onChanged),
          ],
        ),
      ),
    );
  }
}

class _SuggestedActionRow extends StatelessWidget {
  const _SuggestedActionRow({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: PressableTile(
        onTap: onTap,
        pressedScale: 0.985,
        child: FauxGlassCard(
          borderRadius: 18,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 19),
          child: Row(
            children: [
              Icon(icon, color: AppColors.textTertiary, size: 25),
              const SizedBox(width: 16),
              Expanded(
                child: Text(
                  label,
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: AppColors.textSecondary,
                  ),
                ),
              ),
              const Icon(
                Icons.add_rounded,
                color: AppColors.textTertiary,
                size: 21,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
