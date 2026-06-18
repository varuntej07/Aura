import 'package:flutter/material.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import 'aura_consent_screen.dart';

class OnboardingScreen extends StatefulWidget {
  const OnboardingScreen({super.key});

  @override
  State<OnboardingScreen> createState() => _OnboardingScreenState();
}

class _OnboardingScreenState extends State<OnboardingScreen> {
  final _pageController = PageController();
  int _currentPage = 0;

  static const _slides = [
    _OnboardingSlide(
      title: 'Every AI app\nforgets you.',
      body:
          'Aura doesn\'t. It builds a profile from every conversation, automatically, without you doing anything.',
    ),
    _OnboardingSlide(
      title: 'It reaches out\nbefore you ask.',
      body:
          'Morning briefings, reminders, calendar prep. Buddy acts on your schedule, not just when you open the app.',
    ),
    _OnboardingSlide(
      title: '14 days free.\nNo card.',
      body: 'Full access to everything. After that, stay on the free plan or upgrade.',
    ),
    _OnboardingSlide(
      title: 'One quick\nsetup.',
      body: 'Age check and a privacy choice. 30 seconds.',
      isFinal: true,
    ),
  ];

  void _onNext() {
    if (_currentPage < _slides.length - 1) {
      _pageController.nextPage(
        duration: const Duration(milliseconds: 380),
        curve: Curves.easeInOutCubic,
      );
    } else {
      _openConsentScreen();
    }
  }

  void _openConsentScreen() {
    Navigator.of(context).push(
      PageRouteBuilder<void>(
        pageBuilder: (_, animation, _) => const AuraConsentScreen(),
        transitionsBuilder: (_, animation, _, child) => SlideTransition(
          position: Tween(
            begin: const Offset(1.0, 0.0),
            end: Offset.zero,
          ).animate(CurvedAnimation(
            parent: animation,
            curve: Curves.easeInOutCubic,
          )),
          child: child,
        ),
        transitionDuration: const Duration(milliseconds: 320),
      ),
    );
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final bottomPadding = MediaQuery.of(context).viewPadding.bottom;
    final isLastSlide = _currentPage == _slides.length - 1;

    return Scaffold(
      backgroundColor: Colors.transparent,
      body: AmbientBackground(
        child: SafeArea(
          bottom: false,
          child: Column(
            children: [
              // Top bar
              Padding(
                padding: const EdgeInsets.fromLTRB(28, 16, 20, 0),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.spaceBetween,
                  children: [
                    Text(
                      '${_currentPage + 1} / ${_slides.length}',
                      style: const TextStyle(
                        color: AppColors.textTertiary,
                        fontSize: 13,
                        fontWeight: FontWeight.w500,
                        letterSpacing: 0.3,
                      ),
                    ),
                    AnimatedOpacity(
                      opacity: isLastSlide ? 0.0 : 1.0,
                      duration: const Duration(milliseconds: 200),
                      child: GestureDetector(
                        onTap: isLastSlide
                            ? null
                            : () => _pageController.animateToPage(
                                  _slides.length - 1,
                                  duration: const Duration(milliseconds: 450),
                                  curve: Curves.easeInOutCubic,
                                ),
                        child: const Padding(
                          padding: EdgeInsets.all(8),
                          child: Text(
                            'Skip',
                            style: TextStyle(
                              color: AppColors.textTertiary,
                              fontSize: 14,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),

              // Slides
              Expanded(
                child: PageView.builder(
                  controller: _pageController,
                  onPageChanged: (i) => setState(() => _currentPage = i),
                  itemCount: _slides.length,
                  itemBuilder: (_, i) => _SlideView(slide: _slides[i]),
                ),
              ),

              // Dots + button
              Padding(
                padding:
                    EdgeInsets.fromLTRB(28, 12, 28, bottomPadding + 36),
                child: Column(
                  children: [
                    _PageDots(
                      count: _slides.length,
                      current: _currentPage,
                    ),
                    const SizedBox(height: 24),
                    _OnboardingButton(
                      label: isLastSlide ? 'Get started' : 'Continue',
                      onTap: _onNext,
                      isFinal: isLastSlide,
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// Slide data

class _OnboardingSlide {
  final String title;
  final String body;
  final bool isFinal;

  const _OnboardingSlide({
    required this.title,
    required this.body,
    this.isFinal = false,
  });
}

// Slide view - editorial, left-aligned

class _SlideView extends StatelessWidget {
  final _OnboardingSlide slide;

  const _SlideView({required this.slide});

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 28),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Thin accent mark
          Container(
            width: 28,
            height: 2,
            decoration: BoxDecoration(
              color: AppColors.accent,
              borderRadius: BorderRadius.circular(1),
            ),
          ),
          const SizedBox(height: 22),
          Text(
            slide.title,
            style: const TextStyle(
              color: AppColors.textPrimary,
              fontSize: 38,
              fontWeight: FontWeight.w700,
              letterSpacing: -1.5,
              height: 1.05,
            ),
          ),
          const SizedBox(height: 18),
          Text(
            slide.body,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 17,
              height: 1.6,
              letterSpacing: -0.2,
            ),
          ),
        ],
      ),
    );
  }
}

// Page dots

class _PageDots extends StatelessWidget {
  final int count;
  final int current;

  const _PageDots({required this.count, required this.current});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: List.generate(count, (i) {
        final isActive = i == current;
        return AnimatedContainer(
          duration: const Duration(milliseconds: 250),
          curve: Curves.easeInOut,
          margin: const EdgeInsets.symmetric(horizontal: 3),
          width: isActive ? 18 : 5,
          height: 5,
          decoration: BoxDecoration(
            color: isActive
                ? AppColors.accent
                : AppColors.glassBorderLight.withValues(alpha: 0.5),
            borderRadius: BorderRadius.circular(3),
          ),
        );
      }),
    );
  }
}

// CTA button

class _OnboardingButton extends StatelessWidget {
  final String label;
  final VoidCallback onTap;
  final bool isFinal;

  const _OnboardingButton({
    required this.label,
    required this.onTap,
    required this.isFinal,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 250),
        height: 54,
        decoration: BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topLeft,
            end: Alignment.bottomRight,
            colors: isFinal
                ? [AppColors.accent, AppColors.accentDark]
                : [
                    AppColors.accent.withValues(alpha: 0.16),
                    AppColors.accent.withValues(alpha: 0.07),
                  ],
          ),
          borderRadius: BorderRadius.circular(16),
          border: Border.all(
            color: isFinal
                ? Colors.transparent
                : AppColors.accent.withValues(alpha: 0.3),
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
