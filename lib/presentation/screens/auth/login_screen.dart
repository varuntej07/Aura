import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../viewmodels/auth_viewmodel.dart';
import '../../widgets/error_display.dart';
import '../../widgets/loading_indicator.dart';

// ── Screen ────────────────────────────────────────────────────────────────────

enum _EmailFormMode { none, signIn, signUp }

class LoginScreen extends StatefulWidget {
  const LoginScreen({super.key});

  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen>
    with TickerProviderStateMixin {
  late final AnimationController _fadeController;
  late final Animation<double> _nameFade;
  late final Animation<double> _taglineFade;
  late final Animation<double> _buttonsFade;

  final _nameController = TextEditingController();
  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  _EmailFormMode _formMode = _EmailFormMode.none;
  bool _obscurePassword = true;

  String get _loadingMessage =>
      _formMode == _EmailFormMode.signUp ? 'Creating account…' : 'Signing in…';

  @override
  void initState() {
    super.initState();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1600),
    )..forward();
    _nameFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.2, 0.6, curve: Curves.easeOut),
    );
    _taglineFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.4, 0.8, curve: Curves.easeOut),
    );
    _buttonsFade = CurvedAnimation(
      parent: _fadeController,
      curve: const Interval(0.6, 1.0, curve: Curves.easeOut),
    );
  }

  @override
  void dispose() {
    _fadeController.dispose();
    _nameController.dispose();
    _emailController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  void _goBackToOptions(BuildContext context) {
    _nameController.clear();
    _emailController.clear();
    _passwordController.clear();
    context.read<AuthViewModel>().clearError();
    setState(() {
      _formMode = _EmailFormMode.none;
      _obscurePassword = true;
    });
  }

  void _submitSignIn(BuildContext context) {
    final email = _emailController.text.trim();
    final password = _passwordController.text;
    if (email.isEmpty || password.isEmpty) return;
    context.read<AuthViewModel>().signInWithEmail(email, password);
  }

  void _submitSignUp(BuildContext context) {
    final name = _nameController.text.trim();
    final email = _emailController.text.trim();
    final password = _passwordController.text;
    if (name.isEmpty || email.isEmpty || password.isEmpty) return;
    context.read<AuthViewModel>().createAccountWithEmail(email, password, name);
  }

  Widget _buildPasswordToggle() {
    return GestureDetector(
      onTap: () => setState(() => _obscurePassword = !_obscurePassword),
      child: Icon(
        _obscurePassword
            ? Icons.visibility_off_outlined
            : Icons.visibility_outlined,
        color: AppColors.textTertiary,
        size: 20,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AuthViewModel>(
      builder: (context, vm, _) {
        if (vm.state == ViewState.loading) {
          return Scaffold(
            backgroundColor: AppColors.background,
            body: FullScreenLoader(message: _loadingMessage),
          );
        }

        final bottomPadding = MediaQuery.of(context).viewPadding.bottom;

        return Scaffold(
          backgroundColor: AppColors.background,
          body: AmbientBackground(
            child: SingleChildScrollView(
              child: ConstrainedBox(
                constraints: BoxConstraints(
                  minHeight: MediaQuery.of(context).size.height,
                ),
                child: IntrinsicHeight(
                  child: Column(
                    mainAxisAlignment: MainAxisAlignment.center,
                    children: [
                      // Cream hero — wordmark + tagline
                      SizedBox(
                        height: 260,
                        child: Center(
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            children: [
                              FadeTransition(
                                opacity: _nameFade,
                                child: const Text(
                                  'Aura',
                                  style: TextStyle(
                                    fontFamily: 'CormorantGaramond',
                                    fontSize: 72,
                                    fontWeight: FontWeight.w400,
                                    color: AppColors.textPrimary,
                                    letterSpacing: 3,
                                  ),
                                ),
                              ),
                              const SizedBox(height: 6),
                              FadeTransition(
                                opacity: _taglineFade,
                                child: const Text(
                                  'AI that remembers you',
                                  style: TextStyle(
                                    fontFamily: 'JetBrainsMono',
                                    fontSize: 12,
                                    fontWeight: FontWeight.w400,
                                    letterSpacing: 2.0,
                                    color: AppColors.textSecondary,
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),

                      // Auth form
                      FadeTransition(
                        opacity: _buttonsFade,
                        child: Padding(
                          padding: const EdgeInsets.symmetric(horizontal: 28),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.stretch,
                            children: [
                              const SizedBox(height: 8),
                              if (vm.error != null) ...[
                                ErrorDisplay(
                                  error: vm.error!,
                                  onDismiss: vm.clearError,
                                ),
                                const SizedBox(height: 12),
                              ],

                              // ── No email form open ─────────────────────────────
                              if (_formMode == _EmailFormMode.none) ...[
                                _GoogleSignInButton(
                                  onTap: () => context
                                      .read<AuthViewModel>()
                                      .signInWithGoogle(),
                                ),
                                const SizedBox(height: 16),
                                const Center(
                                  child: Text(
                                    'or',
                                    style: TextStyle(
                                      color: AppColors.textTertiary,
                                      fontSize: 13,
                                    ),
                                  ),
                                ),
                                const SizedBox(height: 16),
                                Row(
                                  children: [
                                    Expanded(
                                      child: _EmailActionButton(
                                        label: 'Sign in',
                                        filled: false,
                                        onTap: () => setState(() =>
                                            _formMode = _EmailFormMode.signIn),
                                      ),
                                    ),
                                    const SizedBox(width: 10),
                                    Expanded(
                                      child: _EmailActionButton(
                                        label: 'Create account',
                                        filled: true,
                                        onTap: () => setState(() =>
                                            _formMode = _EmailFormMode.signUp),
                                      ),
                                    ),
                                  ],
                                ),

                              // ── Sign in form ───────────────────────────────────
                              ] else if (_formMode ==
                                  _EmailFormMode.signIn) ...[
                                _BackLink(
                                  onTap: () => _goBackToOptions(context),
                                ),
                                const SizedBox(height: 16),
                                _LoginTextField(
                                  controller: _emailController,
                                  hint: 'Email address',
                                  keyboardType: TextInputType.emailAddress,
                                  textInputAction: TextInputAction.next,
                                ),
                                const SizedBox(height: 10),
                                _LoginTextField(
                                  controller: _passwordController,
                                  hint: 'Password',
                                  obscureText: _obscurePassword,
                                  textInputAction: TextInputAction.done,
                                  onSubmitted: (_) => _submitSignIn(context),
                                  suffixIcon: _buildPasswordToggle(),
                                ),
                                const SizedBox(height: 14),
                                _EmailActionButton(
                                  label: 'Sign in',
                                  icon: Icons.arrow_forward,
                                  filled: true,
                                  onTap: () => _submitSignIn(context),
                                ),

                              // ── Create account form ────────────────────────────
                              ] else ...[
                                _BackLink(
                                  onTap: () => _goBackToOptions(context),
                                ),
                                const SizedBox(height: 16),
                                _LoginTextField(
                                  controller: _nameController,
                                  hint: 'Full name',
                                  keyboardType: TextInputType.name,
                                  textInputAction: TextInputAction.next,
                                ),
                                const SizedBox(height: 10),
                                _LoginTextField(
                                  controller: _emailController,
                                  hint: 'Email address',
                                  keyboardType: TextInputType.emailAddress,
                                  textInputAction: TextInputAction.next,
                                ),
                                const SizedBox(height: 10),
                                _LoginTextField(
                                  controller: _passwordController,
                                  hint: 'Password',
                                  obscureText: _obscurePassword,
                                  textInputAction: TextInputAction.done,
                                  onSubmitted: (_) => _submitSignUp(context),
                                  suffixIcon: _buildPasswordToggle(),
                                ),
                                const SizedBox(height: 14),
                                _EmailActionButton(
                                  label: 'Create account',
                                  icon: Icons.arrow_forward,
                                  filled: true,
                                  onTap: () => _submitSignUp(context),
                                ),
                              ],

                              const SizedBox(height: 20),
                              _LegalFooter(),
                              SizedBox(height: bottomPadding + 16),
                            ],
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        );
      },
    );
  }
}

// Google sign-in button

class _GoogleSignInButton extends StatelessWidget {
  final VoidCallback onTap;
  const _GoogleSignInButton({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 52,
        decoration: BoxDecoration(
          color: AppColors.surface,
          borderRadius: BorderRadius.circular(26),
          border: Border.all(color: AppColors.border),
        ),
        child: const Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Image(
              image: AssetImage('assets/icons/google.png'),
              width: 20,
              height: 20,
            ),
            SizedBox(width: 12),
            Text(
              'Continue with Google',
              style: TextStyle(
                color: AppColors.textPrimary,
                fontSize: 15,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Email action button — filled (accent) or outline (ghost)

class _EmailActionButton extends StatelessWidget {
  final VoidCallback onTap;
  final String label;
  final IconData? icon;
  final bool filled;

  const _EmailActionButton({
    required this.onTap,
    required this.label,
    required this.filled,
    this.icon,
  });

  @override
  Widget build(BuildContext context) {
    final foreground = filled ? Colors.white : AppColors.textPrimary;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        height: 52,
        decoration: BoxDecoration(
          color: filled ? AppColors.accent : Colors.transparent,
          borderRadius: BorderRadius.circular(26),
          border: filled ? null : Border.all(color: AppColors.border),
        ),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            if (icon != null) ...[
              Icon(icon, color: foreground, size: 18),
              const SizedBox(width: 10),
            ],
            Text(
              label,
              style: TextStyle(
                color: foreground,
                fontSize: 15,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Back link

class _BackLink extends StatelessWidget {
  final VoidCallback onTap;
  const _BackLink({required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      behavior: HitTestBehavior.opaque,
      child: const Padding(
        padding: EdgeInsets.only(bottom: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.arrow_back_ios,
              size: 12,
              color: AppColors.textTertiary,
            ),
            SizedBox(width: 4),
            Text(
              'Back',
              style: TextStyle(
                color: AppColors.textTertiary,
                fontSize: 13,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Text field

class _LoginTextField extends StatelessWidget {
  final TextEditingController controller;
  final String hint;
  final bool obscureText;
  final TextInputType? keyboardType;
  final TextInputAction? textInputAction;
  final ValueChanged<String>? onSubmitted;
  final Widget? suffixIcon;

  const _LoginTextField({
    required this.controller,
    required this.hint,
    this.obscureText = false,
    this.keyboardType,
    this.textInputAction,
    this.onSubmitted,
    this.suffixIcon,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: AppColors.surface,
        borderRadius: BorderRadius.circular(26),
        border: Border.all(color: AppColors.border),
      ),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 4),
      child: TextField(
        controller: controller,
        obscureText: obscureText,
        keyboardType: keyboardType,
        textInputAction: textInputAction,
        onSubmitted: onSubmitted,
        cursorColor: AppColors.accent,
        style: const TextStyle(color: AppColors.textPrimary, fontSize: 15),
        decoration: InputDecoration(
          hintText: hint,
          hintStyle: const TextStyle(color: AppColors.textTertiary),
          border: InputBorder.none,
          enabledBorder: InputBorder.none,
          focusedBorder: InputBorder.none,
          contentPadding: const EdgeInsets.symmetric(vertical: 10),
          isDense: true,
          suffixIcon: suffixIcon,
        ),
      ),
    );
  }
}

// Legal footer

class _LegalFooter extends StatelessWidget {
  static final _tosUri = Uri.parse('https://varuntej.dev/aura/terms-of-service');
  static final _privacyUri =
      Uri.parse('https://varuntej.dev/aura/privacy-policy');

  @override
  Widget build(BuildContext context) {
    return Text.rich(
      TextSpan(
        style: const TextStyle(
          color: AppColors.textTertiary,
          fontSize: 11,
        ),
        children: [
          const TextSpan(text: 'By continuing you agree to our '),
          TextSpan(
            text: 'Terms of Service',
            style: TextStyle(
              color: AppColors.accentDark,
              decoration: TextDecoration.underline,
            ),
            recognizer: TapGestureRecognizer()
              ..onTap = () =>
                  launchUrl(_tosUri, mode: LaunchMode.externalApplication),
          ),
          const TextSpan(text: '  ·  '),
          TextSpan(
            text: 'Privacy Policy',
            style: TextStyle(
              color: AppColors.accentDark,
              decoration: TextDecoration.underline,
            ),
            recognizer: TapGestureRecognizer()
              ..onTap = () =>
                  launchUrl(_privacyUri, mode: LaunchMode.externalApplication),
          ),
        ],
      ),
      textAlign: TextAlign.center,
    );
  }
}
