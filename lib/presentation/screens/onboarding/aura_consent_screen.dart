import 'package:flutter/material.dart';
import 'package:flutter/cupertino.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/repositories/onboarding_repository.dart';
import '../../viewmodels/auth_viewmodel.dart';

class AuraConsentScreen extends StatefulWidget {
  const AuraConsentScreen({super.key});

  @override
  State<AuraConsentScreen> createState() => _AuraConsentScreenState();
}

class _AuraConsentScreenState extends State<AuraConsentScreen> {
  // Two internal steps: 0 = age gate, 1 = Aura consent.
  int _step = 0;

  // Age gate state — pre-set to the max allowed date so the picker opens
  // at 2013 and the user can continue without scrolling if that's their year.
  late DateTime _selectedDate;
  String? _ageError;

  // Consent state
  bool _auraConsentGranted = true;
  bool _saving = false;

  static final _minBirthDate = DateTime(1900);

  // Latest allowed DOB: exactly 13 years ago today.
  // The picker's maximumDate is set to this, so years after 2013 are not
  // shown at all — no need to validate under-13 after the user confirms.
  static DateTime get _maxBirthDate {
    final now = DateTime.now();
    return DateTime(now.year - 13, now.month, now.day);
  }

  @override
  void initState() {
    super.initState();
    // Open the picker at the max allowed date (e.g. 2013) so the year limit
    // is immediately visible and youngest users don't have to scroll.
    _selectedDate = _maxBirthDate;
  }

  int get _selectedAge {
    final now = DateTime.now();
    int age = now.year - _selectedDate.year;
    if (now.month < _selectedDate.month ||
        (now.month == _selectedDate.month && now.day < _selectedDate.day)) {
      age--;
    }
    return age;
  }

  void _onDateChanged(DateTime date) {
    setState(() {
      _selectedDate = date;
      _ageError = null;
    });
  }

  void _confirmAgeGate() {
    // Under-13 is impossible cause the picker's maximumDate enforces it.
    // I'm just keeping this check as a guard against edge cases
    if (_selectedAge < 13) {
      setState(() => _ageError = 'Aura isn\'t available for users under 13.');
      return;
    }
    setState(() => _step = 1);
  }

  Future<void> _finalize() async {
    if (_saving) return;
    setState(() => _saving = true);

    final authVm = context.read<AuthViewModel>();
    final repo = context.read<OnboardingRepository>();
    final uid = authVm.user?.uid;

    if (uid == null) {
      setState(() => _saving = false);
      return;
    }

    final age = _selectedAge;
    // Under-18s are never profiled regardless of the toggle.
    final effectiveConsent = age >= 18 ? _auraConsentGranted : false;
    final dob = _selectedDate.toIso8601String().split('T').first;

    final success = await repo.saveOnboardingResult(
      uid: uid,
      dateOfBirth: dob,
      auraConsentGranted: effectiveConsent,
    );

    if (!mounted) return;

    if (success) {
      // Update in-memory model first, then navigate explicitly.
      // AuraConsentScreen was pushed via Navigator (not GoRouter), so we cannot
      // rely on the refreshListenable redirect to clear the mixed stack cleanly.
      authVm.markOnboardingComplete(auraConsentGranted: effectiveConsent);
      if (mounted) context.go('/home');
    } else {
      setState(() => _saving = false);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('Something went wrong. Please try again.'),
          backgroundColor: AppColors.error,
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final bottomPadding = MediaQuery.of(context).viewPadding.bottom;

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          bottom: false,
          child: AnimatedSwitcher(
            duration: const Duration(milliseconds: 320),
            switchInCurve: Curves.easeOutCubic,
            switchOutCurve: Curves.easeInCubic,
            transitionBuilder: (child, animation) {
              return SlideTransition(
                position: Tween(
                  begin: const Offset(0.06, 0.0),
                  end: Offset.zero,
                ).animate(animation),
                child: FadeTransition(opacity: animation, child: child),
              );
            },
            child: _step == 0
                ? _AgeGateStep(
                    key: const ValueKey('age'),
                    selectedDate: _selectedDate,
                    maxDate: _maxBirthDate,
                    minDate: _minBirthDate,
                    error: _ageError,
                    onDateChanged: _onDateChanged,
                    onConfirm: _confirmAgeGate,
                    bottomPadding: bottomPadding,
                  )
                : _ConsentStep(
                    key: const ValueKey('consent'),
                    auraConsentGranted: _auraConsentGranted,
                    isMinor: _selectedAge < 18,
                    saving: _saving,
                    onToggle: (v) => setState(() => _auraConsentGranted = v),
                    onConfirm: _finalize,
                    bottomPadding: bottomPadding,
                  ),
          ),
        ),
      ),
    );
  }
}

// Age gate step

class _AgeGateStep extends StatelessWidget {
  final DateTime selectedDate;
  final DateTime maxDate;
  final DateTime minDate;
  final String? error;
  final ValueChanged<DateTime> onDateChanged;
  final VoidCallback onConfirm;
  final double bottomPadding;

  const _AgeGateStep({
    super.key,
    required this.selectedDate,
    required this.maxDate,
    required this.minDate,
    required this.error,
    required this.onDateChanged,
    required this.onConfirm,
    required this.bottomPadding,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(24, 24, 24, bottomPadding + 32),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Step indicator
          _StepIndicator(current: 0),
          const SizedBox(height: 28),

          // Heading
          const Text(
            'Quick age check',
            style: TextStyle(
              color: AppColors.textPrimary,
              fontSize: 28,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.8,
            ),
          ),
          const SizedBox(height: 10),
          const Text(
            'Required by privacy law. We never share your date of birth with anyone.',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 15,
              height: 1.5,
            ),
          ),

          const SizedBox(height: 32),

          // Date picker
          FauxGlassCard(
            borderRadius: 20,
            child: SizedBox(
              height: 200,
              child: CupertinoTheme(
                data: const CupertinoThemeData(
                  brightness: Brightness.dark,
                  textTheme: CupertinoTextThemeData(
                    dateTimePickerTextStyle: TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 18,
                    ),
                  ),
                ),
                child: CupertinoDatePicker(
                  mode: CupertinoDatePickerMode.date,
                  // Open at maxDate (e.g. 2013-05-11) so years up to 2013
                  // are immediately visible without scrolling.
                  initialDateTime: selectedDate,
                  minimumDate: minDate,
                  maximumDate: maxDate,
                  onDateTimeChanged: onDateChanged,
                ),
              ),
            ),
          ),

          // Error message
          AnimatedSize(
            duration: const Duration(milliseconds: 200),
            child: error != null
                ? Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: FauxGlassCard(
                      borderRadius: 12,
                      padding: const EdgeInsets.symmetric(
                          horizontal: 14, vertical: 10),
                      borderColor: AppColors.error.withValues(alpha: 0.3),
                      gradient: LinearGradient(
                        colors: [
                          AppColors.error.withValues(alpha: 0.10),
                          AppColors.error.withValues(alpha: 0.04),
                        ],
                      ),
                      child: Row(
                        children: [
                          const Icon(Icons.info_outline_rounded,
                              color: AppColors.error, size: 16),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Text(
                              error!,
                              style: const TextStyle(
                                color: AppColors.error,
                                fontSize: 13,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  )
                : const SizedBox.shrink(),
          ),

          const Spacer(),

          _ConsentButton(label: 'Continue', onTap: onConfirm, isFinal: false),
        ],
      ),
    );
  }
}

// Consent step
class _ConsentStep extends StatelessWidget {
  final bool auraConsentGranted;
  final bool isMinor;
  final bool saving;
  final ValueChanged<bool> onToggle;
  final VoidCallback onConfirm;
  final double bottomPadding;

  const _ConsentStep({
    super.key,
    required this.auraConsentGranted,
    required this.isMinor,
    required this.saving,
    required this.onToggle,
    required this.onConfirm,
    required this.bottomPadding,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(24, 24, 24, bottomPadding + 32),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _StepIndicator(current: 1),
          const SizedBox(height: 28),

          const Text(
            'Your memory, your choice',
            style: TextStyle(
              color: AppColors.textPrimary,
              fontSize: 28,
              fontWeight: FontWeight.w700,
              letterSpacing: -0.8,
              height: 1.1,
            ),
          ),
          const SizedBox(height: 10),
          const Text(
            'Aura builds a private profile from your conversations to make Buddy feel like it really knows you.',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 15,
              height: 1.5,
            ),
          ),

          const SizedBox(height: 24),

          // What Aura tracks
          _WhatAuraTracksCard(),

          const SizedBox(height: 20),

          // Consent toggle (disabled for minors)
          FauxGlassCard(
            borderRadius: 16,
            padding: const EdgeInsets.symmetric(horizontal: 4),
            borderColor: isMinor
                ? AppColors.glassBorderDim
                : auraConsentGranted
                    ? AppColors.accent.withValues(alpha: 0.3)
                    : AppColors.glassBorderDim,
            child: SwitchListTile(
              title: Text(
                isMinor ? 'Aura memory (unavailable under 18)' : 'Enable Aura memory',
                style: TextStyle(
                  color: isMinor ? AppColors.textDisabled : AppColors.textPrimary,
                  fontSize: 15,
                ),
              ),
              subtitle: Text(
                isMinor
                    ? 'Behavioral profiling is disabled for users under 18.'
                    : 'You can change this at any time in Settings.',
                style: const TextStyle(
                  color: AppColors.textTertiary,
                  fontSize: 13,
                ),
              ),
              value: isMinor ? false : auraConsentGranted,
              onChanged: isMinor ? null : onToggle,
              activeThumbColor: AppColors.accent,
              activeTrackColor: AppColors.accent.withValues(alpha: 0.3),
            ),
          ),

          const Spacer(),

          saving
              ? const Center(child: CircularProgressIndicator(
                  color: AppColors.accent, strokeWidth: 2))
              : _ConsentButton(
                  label: 'Start using Buddy',
                  onTap: onConfirm,
                  isFinal: true,
                ),

          const SizedBox(height: 14),
          Center(
            child: Column(
              children: [
                const Text(
                  'Your data is never sold. GDPR compliant.',
                  style: TextStyle(
                    color: AppColors.textTertiary,
                    fontSize: 11,
                  ),
                  textAlign: TextAlign.center,
                ),
                const SizedBox(height: 6),
                Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    _LegalLink(
                      label: 'Privacy Policy',
                      url: 'https://varuntej.dev/aura/privacy-policy',
                    ),
                    const Text(
                      ' · ',
                      style: TextStyle(color: AppColors.textTertiary, fontSize: 11),
                    ),
                    _LegalLink(
                      label: 'Terms of Service',
                      url: 'https://varuntej.dev/aura/terms-of-service',
                    ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

// What Aura tracks card
class _WhatAuraTracksCard extends StatelessWidget {
  static const _items = [
    (Icons.interests_outlined, 'Your topics and interests across chats'),
    (Icons.tune_outlined, 'How you like Buddy to respond'),
    (Icons.flag_outlined, 'Goals you mention — like fitness or learning'),
  ];

  static const _notItems = [
    'Your actual messages',
    'Passwords or financial data',
    'Location',
  ];

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.section(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'What Aura remembers',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 12,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.8,
            ),
          ),
          const SizedBox(height: 12),
          ..._items.map((item) => Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Icon(item.$1, color: AppColors.accent, size: 16),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        item.$2,
                        style: const TextStyle(
                          color: AppColors.textPrimary,
                          fontSize: 13,
                          height: 1.4,
                        ),
                      ),
                    ),
                  ],
                ),
              )),
          const Divider(color: AppColors.glassBorderDim, height: 20),
          const Text(
            'Never stored',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 12,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.8,
            ),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: _notItems.map((label) {
              return FauxGlassCard(
                borderRadius: 8,
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
                child: Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    const Icon(Icons.block_outlined,
                        color: AppColors.textTertiary, size: 12),
                    const SizedBox(width: 5),
                    Text(
                      label,
                      style: const TextStyle(
                        color: AppColors.textTertiary,
                        fontSize: 12,
                      ),
                    ),
                  ],
                ),
              );
            }).toList(),
          ),
        ],
      ),
    );
  }
}

// Step indicator
class _StepIndicator extends StatelessWidget {
  final int current;

  const _StepIndicator({required this.current});

  @override
  Widget build(BuildContext context) {
    return Row(
      children: List.generate(2, (i) {
        final isActive = i == current;
        final isDone = i < current;
        return Expanded(
          child: Padding(
            padding: EdgeInsets.only(right: i < 1 ? 8 : 0),
            child: AnimatedContainer(
              duration: const Duration(milliseconds: 300),
              height: 3,
              decoration: BoxDecoration(
                color: isActive || isDone
                    ? AppColors.accent
                    : AppColors.glassBorderDim,
                borderRadius: BorderRadius.circular(2),
              ),
            ),
          ),
        );
      }),
    );
  }
}

// Legal link
class _LegalLink extends StatelessWidget {
  final String label;
  final String url;

  const _LegalLink({required this.label, required this.url});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: () => launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication),
      child: Text(
        label,
        style: const TextStyle(
          color: AppColors.textTertiary,
          fontSize: 11,
          decoration: TextDecoration.underline,
          decorationColor: AppColors.textTertiary,
        ),
      ),
    );
  }
}

// Shared button
class _ConsentButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  final bool isFinal;

  const _ConsentButton({
    required this.label,
    required this.onTap,
    required this.isFinal,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 54,
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: isFinal
                ? [AppColors.accent, AppColors.accentDark]
                : [
                    AppColors.accent.withValues(alpha: 0.18),
                    AppColors.accent.withValues(alpha: 0.08),
                  ],
          ),
          borderRadius: BorderRadius.circular(16),
          border: Border.all(
            color: AppColors.accent.withValues(alpha: isFinal ? 0.0 : 0.35),
          ),
        ),
        child: Center(
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                label,
                style: TextStyle(
                  color: isFinal ? Colors.black : AppColors.accent,
                  fontSize: 16,
                  fontWeight: FontWeight.w600,
                  letterSpacing: -0.2,
                ),
              ),
              const SizedBox(width: 8),
              Icon(
                Icons.arrow_forward_rounded,
                color: isFinal ? Colors.black : AppColors.accent,
                size: 18,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
