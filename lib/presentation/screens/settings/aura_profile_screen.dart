import 'package:cloud_firestore/cloud_firestore.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../widgets/sign_in_required_view.dart';
import '../onboarding/aura_consent_screen.dart';

class AuraProfileScreen extends StatelessWidget {
  const AuraProfileScreen({super.key});

  static Route<void> route() => MaterialPageRoute(
        builder: (_) => const AuraProfileScreen(),
      );

  @override
  Widget build(BuildContext context) {
    final authVm = context.watch<AuthViewModel>();
    final uid = authVm.user?.uid;
    if (uid == null) {
      // Guest (logged-out) user reached this via Settings. Offer a real way in
      // instead of a dead-end message.
      return Scaffold(
        backgroundColor: Colors.transparent,
        body: AmbientBackground(
          child: SafeArea(
            child: Column(
              children: [
                Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                  child: Row(
                    children: [
                      GlassIconButton(
                        icon: Icons.arrow_back_ios_new,
                        onTap: () => Navigator.pop(context),
                        iconSize: 17,
                      ),
                    ],
                  ),
                ),
                Expanded(
                  child: SignInRequiredView(
                    icon: Icons.auto_awesome_outlined,
                    message: 'Sign in to see what Buddy has learned about you.',
                    onSignIn: () => context.go('/login'),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    }
    // Memory is only built (and only readable) with explicit consent. When it is
    // off — never granted, or later turned off — show the invitation to turn it
    // on instead of an empty profile, since there is nothing to display.
    final memoryEnabled = authVm.auraMemoryEnabled;

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
                child: Row(
                  children: [
                    GlassIconButton(
                      icon: Icons.arrow_back_ios_new,
                      onTap: () => Navigator.pop(context),
                      iconSize: 17,
                    ),
                    const SizedBox(width: 14),
                    const Text(
                      'Your Aura',
                      style: TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 20,
                        fontWeight: FontWeight.w700,
                        letterSpacing: -0.5,
                      ),
                    ),
                  ],
                ),
              ),
              Expanded(
                child: memoryEnabled
                    ? StreamBuilder<DocumentSnapshot>(
                        stream: FirebaseFirestore.instance
                            .collection('UserAura')
                            .doc(uid)
                            .snapshots(),
                        builder: (context, snapshot) {
                          if (snapshot.connectionState ==
                              ConnectionState.waiting) {
                            return const Center(
                              child: CircularProgressIndicator(
                                color: AppColors.accent,
                                strokeWidth: 2,
                              ),
                            );
                          }

                          // A read error (permission denied / Firestore outage) must
                          // not masquerade as an empty profile — show a distinct,
                          // retryable error state instead of _EmptyAuraProfile.
                          if (snapshot.hasError) {
                            return const _ErrorBody(
                              message:
                                  "Couldn't load your Aura right now. Try again in a sec.",
                            );
                          }

                          final data =
                              snapshot.data?.data() as Map<String, dynamic>?;
                          if (data == null || data.isEmpty) {
                            return const _EmptyAuraProfile();
                          }

                          return _AuraProfileBody(profile: data);
                        },
                      )
                    : const _AuraMemoryOffPrompt(),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// Profile body — renders each non-empty section

class _AuraProfileBody extends StatelessWidget {
  final Map<String, dynamic> profile;

  const _AuraProfileBody({required this.profile});

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 32),
      children: [
        // Lead with the narrative (the connected story), then the flatter lists.
        _StorylinesSection(summaries: _StorylinesSection.parse(profile['storylines'])),
        _ExplicitFactsSection(facts: _strings(profile['explicit_facts'])),
        _GoalsSection(goals: _strings(profile['inferred_goals'])),
        _InterestsSection(
          entries: _InterestsSection.parse(
            profile['interests'],
            profile['deep_interest_frequencies'],
          ),
        ),
        _ToneSection(
          tone: profile['dominant_tone'] as String?,
          depthPref: profile['response_depth_preference'] as String?,
        ),
        _TraitsSection(traits: _TraitsSection.parse(profile['traits'])),
        _DietarySection(patterns: _strings(profile['dietary_patterns'])),
        _StyleSection(
          prefer: _strings(profile['response_style_prefer']),
          avoid: _strings(profile['response_style_avoid']),
        ),
        const SizedBox(height: 16),
        const _ProfileFootnote(),
      ],
    );
  }

  static List<String> _strings(dynamic value) {
    if (value is List) return value.whereType<String>().toList();
    return [];
  }
}

// Section: explicit facts (things you've told Buddy directly)

class _ExplicitFactsSection extends StatelessWidget {
  final List<String> facts;
  const _ExplicitFactsSection({required this.facts});

  @override
  Widget build(BuildContext context) {
    if (facts.isEmpty) return const SizedBox.shrink();
    return _ProfileSection(
      icon: Icons.person_outline_rounded,
      label: 'What you\'ve shared',
      child: Column(
        children: facts
            .map((f) => _BulletRow(text: f))
            .toList(),
      ),
    );
  }
}

// Section: inferred goals

class _GoalsSection extends StatelessWidget {
  final List<String> goals;
  const _GoalsSection({required this.goals});

  @override
  Widget build(BuildContext context) {
    if (goals.isEmpty) return const SizedBox.shrink();
    return _ProfileSection(
      icon: Icons.flag_outlined,
      label: 'Goals Buddy has noticed',
      child: Column(
        children: goals.map((g) => _BulletRow(text: g)).toList(),
      ),
    );
  }
}

// Section: storylines — the narrative layer (what's going on in the user's life),
// fused across messages by the per-session reflection tier. Newest/strongest first.

class _StorylinesSection extends StatelessWidget {
  final List<String> summaries;
  const _StorylinesSection({required this.summaries});

  /// Parse the `storylines` map ({id: {summary, weight, ...}}) into summary lines
  /// ranked by stored weight. Tolerates a missing/old-shape field (returns []).
  static List<String> parse(dynamic storylines) {
    if (storylines is! Map || storylines.isEmpty) return const [];
    final rows = <MapEntry<String, double>>[];
    storylines.forEach((_, node) {
      if (node is Map && node['summary'] != null) {
        rows.add(MapEntry(
          node['summary'].toString(),
          (node['weight'] as num?)?.toDouble() ?? 0,
        ));
      }
    });
    rows.sort((a, b) => b.value.compareTo(a.value));
    return rows.take(6).map((e) => e.key).toList();
  }

  @override
  Widget build(BuildContext context) {
    if (summaries.isEmpty) return const SizedBox.shrink();
    return _ProfileSection(
      icon: Icons.auto_stories_outlined,
      label: 'What\'s going on',
      child: Column(
        children: summaries.map((s) => _BulletRow(text: s)).toList(),
      ),
    );
  }
}

// Section: traits — corroborated personality signals. Mirrors the backend
// `shown_traits` gate so an uncorroborated or low-confidence guess is never displayed.

class _TraitsSection extends StatelessWidget {
  final List<String> traits;
  const _TraitsSection({required this.traits});

  static const _minEvidence = 2;       // == backend TRAIT_MIN_EVIDENCE
  static const _minConfidence = 0.7;   // == backend TRAIT_MIN_CONFIDENCE

  static List<String> parse(dynamic raw) {
    if (raw is! Map || raw.isEmpty) return const [];
    final rows = <MapEntry<String, double>>[];
    raw.forEach((key, node) {
      if (node is! Map) return;
      final evidence = (node['evidence_count'] as num?)?.toInt() ?? 0;
      final confidence = (node['confidence'] as num?)?.toDouble() ?? 0;
      if (evidence < _minEvidence || confidence < _minConfidence) return;
      rows.add(MapEntry(
        (node['display'] ?? key).toString(),
        (node['weight'] as num?)?.toDouble() ?? 0,
      ));
    });
    rows.sort((a, b) => b.value.compareTo(a.value));
    return rows.take(6).map((e) => e.key).toList();
  }

  @override
  Widget build(BuildContext context) {
    if (traits.isEmpty) return const SizedBox.shrink();
    return _ProfileSection(
      icon: Icons.emoji_objects_outlined,
      label: 'What Buddy senses about you',
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        children: traits.map((t) => _InterestChip(label: t)).toList(),
      ),
    );
  }
}

// Section: top interest categories with the specific subjects inside them

class _InterestEntry {
  final String label;
  final List<String> subjects;
  final double weight;
  const _InterestEntry(this.label, this.subjects, this.weight);
}

class _InterestsSection extends StatelessWidget {
  final List<_InterestEntry> entries;
  const _InterestsSection({required this.entries});

  /// Parse the nested `interests` map ({category: {weight, subjects: {...}}}).
  /// Falls back to the legacy flat `deep_interest_frequencies` map for profiles
  /// that have not rebuilt into the new structure yet.
  static List<_InterestEntry> parse(dynamic interests, dynamic legacy) {
    if (interests is Map && interests.isNotEmpty) {
      final result = <_InterestEntry>[];
      interests.forEach((slug, node) {
        if (node is! Map) return;
        final weight = (node['weight'] as num?)?.toDouble() ?? 0;
        if (weight <= 0) return;
        final subjects = <MapEntry<String, double>>[];
        final raw = node['subjects'];
        if (raw is Map) {
          raw.forEach((key, sv) {
            if (sv is Map) {
              subjects.add(MapEntry(
                (sv['display'] ?? key).toString(),
                (sv['weight'] as num?)?.toDouble() ?? 0,
              ));
            }
          });
        }
        subjects.sort((a, b) => b.value.compareTo(a.value));
        result.add(_InterestEntry(
          _prettySlug(slug.toString()),
          subjects.take(4).map((e) => e.key).toList(),
          weight,
        ));
      });
      result.sort((a, b) => b.weight.compareTo(a.weight));
      return result.take(6).toList();
    }

    if (legacy is Map && legacy.isNotEmpty) {
      final result = legacy.entries
          .map((e) => _InterestEntry(
                e.key.toString(),
                const [],
                (e.value as num?)?.toDouble() ?? 0,
              ))
          .toList()
        ..sort((a, b) => b.weight.compareTo(a.weight));
      return result.take(6).toList();
    }
    return [];
  }

  static String _prettySlug(String slug) {
    if (slug.isEmpty) return slug;
    final words = slug.replaceAll('_', ' ');
    return words[0].toUpperCase() + words.substring(1);
  }

  @override
  Widget build(BuildContext context) {
    if (entries.isEmpty) return const SizedBox.shrink();

    return _ProfileSection(
      icon: Icons.interests_outlined,
      label: 'Topics you care about',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (var i = 0; i < entries.length; i++) ...[
            if (i > 0) const SizedBox(height: 12),
            Text(
              entries[i].label,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 14,
                fontWeight: FontWeight.w600,
              ),
            ),
            if (entries[i].subjects.isNotEmpty) ...[
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: entries[i]
                    .subjects
                    .map((s) => _InterestChip(label: s))
                    .toList(),
              ),
            ],
          ],
        ],
      ),
    );
  }
}

// Section: tone and depth preference

class _ToneSection extends StatelessWidget {
  final String? tone;
  final String? depthPref;

  const _ToneSection({required this.tone, required this.depthPref});

  static const _toneLabels = {
    'casual': 'Casual',
    'terse': 'Brief and direct',
    'verbose': 'Detailed',
    'formal': 'Formal',
    'playful': 'Playful',
  };

  static const _depthLabels = {
    'wants_brief': 'Prefers short answers',
    'wants_detailed': 'Prefers thorough explanations',
    'wants_step_by_step': 'Prefers step-by-step breakdowns',
    'wants_examples': 'Learns better with examples',
    'wants_opinion': 'Wants direct recommendations',
  };

  @override
  Widget build(BuildContext context) {
    final toneLabel = _toneLabels[tone];
    final depthLabel = _depthLabels[depthPref];
    if (toneLabel == null && depthLabel == null) return const SizedBox.shrink();

    return _ProfileSection(
      icon: Icons.tune_outlined,
      label: 'How you like to be spoken to',
      child: Column(
        children: [
          if (toneLabel != null) _BulletRow(text: 'Tone: $toneLabel'),
          if (depthLabel != null) _BulletRow(text: depthLabel),
        ],
      ),
    );
  }
}

// Section: dietary patterns

class _DietarySection extends StatelessWidget {
  final List<String> patterns;
  const _DietarySection({required this.patterns});

  @override
  Widget build(BuildContext context) {
    if (patterns.isEmpty) return const SizedBox.shrink();
    return _ProfileSection(
      icon: Icons.restaurant_outlined,
      label: 'Dietary patterns',
      child: Column(
        children: patterns.map((p) => _BulletRow(text: p)).toList(),
      ),
    );
  }
}

// Section: style prefer/avoid derived from feedback signals

class _StyleSection extends StatelessWidget {
  final List<String> prefer;
  final List<String> avoid;

  const _StyleSection({required this.prefer, required this.avoid});

  @override
  Widget build(BuildContext context) {
    if (prefer.isEmpty && avoid.isEmpty) return const SizedBox.shrink();
    return _ProfileSection(
      icon: Icons.psychology_outlined,
      label: 'Response style signals',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (prefer.isNotEmpty) ...[
            _SubLabel('Buddy should'),
            ...prefer.map((p) => _BulletRow(text: p, color: AppColors.accent)),
          ],
          if (avoid.isNotEmpty) ...[
            if (prefer.isNotEmpty) const SizedBox(height: 8),
            _SubLabel('Buddy should avoid'),
            ...avoid.map((a) => _BulletRow(text: a, color: AppColors.textTertiary)),
          ],
        ],
      ),
    );
  }
}

// Shared section container

class _ProfileSection extends StatelessWidget {
  final IconData icon;
  final String label;
  final Widget child;

  const _ProfileSection({
    required this.icon,
    required this.label,
    required this.child,
  });

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: FauxGlassCard.section(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, size: 15, color: AppColors.accent),
                const SizedBox(width: 8),
                Text(
                  label.toUpperCase(),
                  style: const TextStyle(
                    color: AppColors.textTertiary,
                    fontSize: 10,
                    fontWeight: FontWeight.w600,
                    letterSpacing: 1.0,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            child,
          ],
        ),
      ),
    );
  }
}

class _BulletRow extends StatelessWidget {
  final String text;
  final Color color;

  const _BulletRow({required this.text, this.color = AppColors.textPrimary});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Container(
              width: 4,
              height: 4,
              decoration: BoxDecoration(
                color: AppColors.accent.withValues(alpha: 0.6),
                shape: BoxShape.circle,
              ),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              text,
              style: TextStyle(color: color, fontSize: 14, height: 1.4),
            ),
          ),
        ],
      ),
    );
  }
}

class _SubLabel extends StatelessWidget {
  final String text;
  const _SubLabel(this.text);

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        text,
        style: const TextStyle(
          color: AppColors.textSecondary,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _InterestChip extends StatelessWidget {
  final String label;

  const _InterestChip({required this.label});

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard.pill(
      child: Text(
        label,
        style: const TextStyle(color: AppColors.textPrimary, fontSize: 13),
      ),
    );
  }
}

// Memory-off invitation — shown when Aura memory is not enabled (never granted
// or later turned off). The "turn on" action opens the consent screen (age gate
// + full disclosure), never a bare write, so consent stays informed and the
// under-18 rule is enforced there.

class _AuraMemoryOffPrompt extends StatelessWidget {
  const _AuraMemoryOffPrompt();

  static const _remembers = [
    'Your topics and interests across chats',
    'How you like Buddy to respond',
    'Goals you mention, like fitness or learning',
  ];

  static const _neverStored = [
    'Your actual messages',
    'Passwords or financial data',
    'Location',
  ];

  void _turnOn(BuildContext context) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(builder: (_) => const AuraConsentScreen()),
    );
  }

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 32),
      children: [
        const SizedBox(height: 8),
        Center(
          child: Container(
            width: 72,
            height: 72,
            decoration: BoxDecoration(
              color: AppColors.accent.withValues(alpha: 0.08),
              shape: BoxShape.circle,
            ),
            child: const Icon(
              Icons.auto_awesome_outlined,
              size: 32,
              color: AppColors.accent,
            ),
          ),
        ),
        const SizedBox(height: 20),
        const Text(
          'Let Buddy remember you',
          textAlign: TextAlign.center,
          style: TextStyle(
            color: AppColors.textPrimary,
            fontSize: 22,
            fontWeight: FontWeight.w700,
            letterSpacing: -0.5,
          ),
        ),
        const SizedBox(height: 10),
        const Text(
          'Aura memory is off, so Buddy is starting fresh every time. Turn it on '
          'and Buddy quietly learns what matters to you, so chats, briefings, and '
          'nudges actually feel like they know you.',
          textAlign: TextAlign.center,
          style: TextStyle(
            color: AppColors.textSecondary,
            fontSize: 14,
            height: 1.6,
          ),
        ),
        const SizedBox(height: 24),
        FauxGlassCard.section(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const _DisclosureLabel('What Aura remembers'),
              const SizedBox(height: 10),
              ..._remembers.map((t) => _DisclosureRow(text: t, included: true)),
              const Divider(color: AppColors.glassBorderDim, height: 22),
              const _DisclosureLabel('Never stored'),
              const SizedBox(height: 10),
              ..._neverStored.map((t) => _DisclosureRow(text: t, included: false)),
            ],
          ),
        ),
        const SizedBox(height: 20),
        GestureDetector(
          onTap: () => _turnOn(context),
          child: Container(
            height: 54,
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [AppColors.accent, AppColors.accentDark],
              ),
              borderRadius: BorderRadius.circular(16),
            ),
            child: const Center(
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    'Turn on Aura memory',
                    style: TextStyle(
                      color: Colors.black,
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                      letterSpacing: -0.2,
                    ),
                  ),
                  SizedBox(width: 8),
                  Icon(Icons.arrow_forward_rounded, color: Colors.black, size: 18),
                ],
              ),
            ),
          ),
        ),
        const SizedBox(height: 14),
        const Text(
          'You stay in control. Turn it off anytime in Settings. Your data is '
          'never sold. GDPR compliant.',
          textAlign: TextAlign.center,
          style: TextStyle(
            color: AppColors.textTertiary,
            fontSize: 11,
            height: 1.6,
          ),
        ),
      ],
    );
  }
}

class _DisclosureLabel extends StatelessWidget {
  final String text;
  const _DisclosureLabel(this.text);

  @override
  Widget build(BuildContext context) {
    return Text(
      text.toUpperCase(),
      style: const TextStyle(
        color: AppColors.textSecondary,
        fontSize: 11,
        fontWeight: FontWeight.w600,
        letterSpacing: 0.8,
      ),
    );
  }
}

class _DisclosureRow extends StatelessWidget {
  final String text;
  final bool included;
  const _DisclosureRow({required this.text, required this.included});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            included ? Icons.check_rounded : Icons.block_outlined,
            size: 16,
            color: included ? AppColors.accent : AppColors.textTertiary,
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              text,
              style: TextStyle(
                color: included ? AppColors.textPrimary : AppColors.textTertiary,
                fontSize: 13,
                height: 1.4,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// Empty state — shown when no profile has been built yet

class _EmptyAuraProfile extends StatelessWidget {
  const _EmptyAuraProfile();

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          Container(
            width: 72,
            height: 72,
            decoration: BoxDecoration(
              color: AppColors.accent.withValues(alpha: 0.08),
              shape: BoxShape.circle,
            ),
            child: const Icon(
              Icons.auto_awesome_outlined,
              size: 32,
              color: AppColors.accent,
            ),
          ),
          const SizedBox(height: 20),
          const Text(
            'Aura is still learning',
            style: TextStyle(
              color: AppColors.textPrimary,
              fontSize: 18,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 10),
          const Text(
            'Chat with Buddy for a while and your Aura profile will start filling in: your interests, goals, tone, and everything Buddy learns about you.',
            style: TextStyle(
              color: AppColors.textSecondary,
              fontSize: 14,
              height: 1.6,
            ),
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }
}

class _ProfileFootnote extends StatelessWidget {
  const _ProfileFootnote();

  @override
  Widget build(BuildContext context) {
    return const Text(
      'This profile is built silently from your conversations. It is never sold or shared. You can disable it anytime in Settings.',
      style: TextStyle(
        color: AppColors.textTertiary,
        fontSize: 11,
        height: 1.6,
      ),
      textAlign: TextAlign.center,
    );
  }
}

class _ErrorBody extends StatelessWidget {
  final String message;
  const _ErrorBody({required this.message});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      body: Center(
        child: Text(message,
            style: const TextStyle(color: AppColors.textSecondary)),
      ),
    );
  }
}
