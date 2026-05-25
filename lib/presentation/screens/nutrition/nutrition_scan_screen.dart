import 'dart:io';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:provider/provider.dart';

import '../../../core/theme/app_colors.dart';
import '../../../core/theme/glass_card.dart';
import '../../../data/models/dietary_profile_model.dart';
import '../../../data/models/scan_result_model.dart';
import '../../viewmodels/dietary_profile_viewmodel.dart';
import '../../viewmodels/nutrition_scan_viewmodel.dart';

class NutritionScanScreen extends StatefulWidget {
  const NutritionScanScreen({super.key});

  @override
  State<NutritionScanScreen> createState() => _NutritionScanScreenState();
}

class _NutritionScanScreenState extends State<NutritionScanScreen>
    with WidgetsBindingObserver {
  final _picker = ImagePicker();
  File? _pickedImage;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) {
      context.read<NutritionScanViewModel>().reset();
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    context.read<NutritionScanViewModel>().onAppLifecycleChanged(state);
  }

  Future<void> _openCamera() async {
    final xfile = await _picker.pickImage(
      source: ImageSource.camera,
      imageQuality: 85,
      maxWidth: 1920,
    );
    if (xfile == null || !mounted) return;
    setState(() => _pickedImage = File(xfile.path));
  }

  Future<void> _pickGallery() async {
    final xfile = await _picker.pickImage(
      source: ImageSource.gallery,
      imageQuality: 85,
      maxWidth: 1920,
    );
    if (xfile == null || !mounted) return;
    setState(() => _pickedImage = File(xfile.path));
  }

  Future<void> _scan() async {
    if (_pickedImage == null) return;
    await context.read<NutritionScanViewModel>().scan(_pickedImage!);
  }

  void _showProfileSheet() {
    final profile = context.read<DietaryProfileViewModel>().profile;
    showModalBottomSheet(
      context: context,
      backgroundColor: Colors.transparent,
      isScrollControlled: true,
      builder: (_) => _ProfileSheet(profile: profile),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: AppColors.deepBackground,
      extendBodyBehindAppBar: true,
      appBar: AppBar(
        backgroundColor: Colors.transparent,
        elevation: 0,
        surfaceTintColor: Colors.transparent,
        automaticallyImplyLeading: false,
        titleSpacing: 16,
        title: Row(
          children: [
            GlassIconButton(
              icon: Icons.arrow_back_rounded,
              onTap: () => Navigator.of(context).pop(),
            ),
            const SizedBox(width: 12),
            const Text(
              'Nutrition Scanner',
              style: TextStyle(
                color: AppColors.textPrimary,
                fontSize: 17,
                fontWeight: FontWeight.w600,
              ),
            ),
          ],
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 16),
            child: GlassIconButton(
              icon: Icons.person_outline_rounded,
              onTap: _showProfileSheet,
            ),
          ),
        ],
      ),
      body: AmbientBackground(
        child: Consumer<NutritionScanViewModel>(
          builder: (context, vm, _) {
            return switch (vm.state) {
              NutritionScanState.idle => _IdleView(
                  image: _pickedImage,
                  onCamera: _openCamera,
                  onGallery: _pickGallery,
                  onScan: _pickedImage != null ? _scan : null,
                ),
              NutritionScanState.scanning ||
              NutritionScanState.analyzing =>
                _LoadingView(phrase: vm.currentLoadingPhrase),
              NutritionScanState.questioning => _QuestionView(vm: vm),
              NutritionScanState.result => _ResultView(
                  analysis: vm.analysis!,
                  onReset: () {
                    vm.reset();
                    setState(() => _pickedImage = null);
                  },
                ),
              NutritionScanState.error => _ErrorView(
                  message: 'Something went wrong. Try again in a moment.',
                  onRetry: vm.reset,
                ),
            };
          },
        ),
      ),
    );
  }
}

// Idle: image zone + camera/gallery/scan

class _IdleView extends StatelessWidget {
  final File? image;
  final VoidCallback onCamera;
  final VoidCallback onGallery;
  final VoidCallback? onScan;

  const _IdleView({
    required this.image,
    required this.onCamera,
    required this.onGallery,
    required this.onScan,
  });

  @override
  Widget build(BuildContext context) {
    final topPad = MediaQuery.of(context).padding.top + kToolbarHeight + 12;
    final bottomPad = MediaQuery.of(context).padding.bottom + 20;
    return Padding(
      padding: EdgeInsets.fromLTRB(20, topPad, 20, bottomPad),
      child: Column(
        children: [
          Expanded(child: _ImageZone(image: image)),
          const SizedBox(height: 16),
          Row(
            children: [
              Expanded(
                child: _GlassActionButton(
                  icon: Icons.camera_alt_rounded,
                  label: 'Camera',
                  onTap: onCamera,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: _GlassActionButton(
                  icon: Icons.photo_library_rounded,
                  label: 'Gallery',
                  onTap: onGallery,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          _ScanButton(onScan: onScan),
        ],
      ),
    );
  }
}

class _ImageZone extends StatelessWidget {
  final File? image;
  const _ImageZone({required this.image});

  @override
  Widget build(BuildContext context) {
    if (image != null) {
      return ClipRRect(
        borderRadius: BorderRadius.circular(24),
        child: Stack(
          fit: StackFit.expand,
          children: [
            Image.file(image!, fit: BoxFit.cover),
            Positioned(
              bottom: 0,
              left: 0,
              right: 0,
              child: Container(
                padding: const EdgeInsets.fromLTRB(16, 32, 16, 16),
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.bottomCenter,
                    end: Alignment.topCenter,
                    colors: [
                      Colors.black.withValues(alpha: 0.72),
                      Colors.transparent,
                    ],
                  ),
                ),
                child: Row(
                  children: [
                    Container(
                      width: 8,
                      height: 8,
                      decoration: const BoxDecoration(
                        shape: BoxShape.circle,
                        color: AppColors.success,
                        boxShadow: [
                          BoxShadow(
                            color: AppColors.success,
                            blurRadius: 6,
                            spreadRadius: 1,
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 8),
                    const Text(
                      'Ready to scan',
                      style: TextStyle(
                        color: Colors.white,
                        fontSize: 13,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      );
    }

    return FauxGlassCard(
      borderRadius: 24,
      borderColor: AppColors.glassBorderDim,
      child: const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              Icons.camera_alt_rounded,
              color: AppColors.textTertiary,
              size: 52,
            ),
            SizedBox(height: 16),
            Text(
              'Take a photo of your food',
              style: TextStyle(
                color: AppColors.textSecondary,
                fontSize: 15,
                fontWeight: FontWeight.w500,
              ),
            ),
            SizedBox(height: 6),
            Text(
              'Labels, dishes, or packaged food',
              style: TextStyle(color: AppColors.textTertiary, fontSize: 13),
            ),
          ],
        ),
      ),
    );
  }
}

class _GlassActionButton extends StatelessWidget {
  final IconData icon;
  final String label;
  final VoidCallback onTap;

  const _GlassActionButton({
    required this.icon,
    required this.label,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: FauxGlassCard(
        borderRadius: 14,
        borderColor: AppColors.glassBorderLight,
        padding: const EdgeInsets.symmetric(vertical: 14),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Icon(icon, color: AppColors.textSecondary, size: 18),
            const SizedBox(width: 8),
            Text(
              label,
              style: const TextStyle(
                color: AppColors.textPrimary,
                fontSize: 14,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _ScanButton extends StatelessWidget {
  final VoidCallback? onScan;
  const _ScanButton({required this.onScan});

  @override
  Widget build(BuildContext context) {
    final active = onScan != null;
    return GestureDetector(
      onTap: onScan,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 260),
        curve: Curves.easeOutCubic,
        width: double.infinity,
        height: 56,
        decoration: BoxDecoration(
          gradient: active
              ? LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: [AppColors.accent, AppColors.accentDark],
                )
              : null,
          color: active ? null : const Color(0x14FFFFFF),
          borderRadius: BorderRadius.circular(16),
          border: Border.all(
            color: active
                ? AppColors.accent.withValues(alpha: 0.55)
                : AppColors.glassBorderDim,
            width: 1,
          ),
          boxShadow: active
              ? [
                  BoxShadow(
                    color: AppColors.accent.withValues(alpha: 0.40),
                    blurRadius: 22,
                    offset: const Offset(0, 7),
                  ),
                ]
              : null,
        ),
        child: Center(
          child: AnimatedDefaultTextStyle(
            duration: const Duration(milliseconds: 200),
            style: TextStyle(
              color: active ? Colors.white : AppColors.textTertiary,
              fontSize: 16,
              fontWeight: FontWeight.w700,
            ),
            child: const Text('Scan'),
          ),
        ),
      ),
    );
  }
}

// Loading: pulsing orb + cycling phrases
class _LoadingView extends StatefulWidget {
  final String phrase;
  const _LoadingView({required this.phrase});

  @override
  State<_LoadingView> createState() => _LoadingViewState();
}

class _LoadingViewState extends State<_LoadingView>
    with SingleTickerProviderStateMixin {
  late final AnimationController _pulse;
  late final Animation<double> _scale;
  late final Animation<double> _glow;

  @override
  void initState() {
    super.initState();
    _pulse = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 1600),
    )..repeat(reverse: true);
    _scale = Tween<double>(begin: 0.85, end: 1.15).animate(
      CurvedAnimation(parent: _pulse, curve: Curves.easeInOut),
    );
    _glow = Tween<double>(begin: 0.35, end: 0.90).animate(
      CurvedAnimation(parent: _pulse, curve: Curves.easeInOut),
    );
  }

  @override
  void dispose() {
    _pulse.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final topPad = MediaQuery.of(context).padding.top + kToolbarHeight;
    return Padding(
      padding: EdgeInsets.only(top: topPad),
      child: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            AnimatedBuilder(
              animation: _pulse,
              builder: (_, _) => Transform.scale(
                scale: _scale.value,
                child: Container(
                  width: 80,
                  height: 80,
                  decoration: BoxDecoration(
                    shape: BoxShape.circle,
                    gradient: RadialGradient(
                      colors: [
                        AppColors.accent.withValues(alpha: _glow.value * 0.85),
                        AppColors.accent.withValues(alpha: _glow.value * 0.15),
                      ],
                    ),
                    boxShadow: [
                      BoxShadow(
                        color:
                            AppColors.accent.withValues(alpha: _glow.value * 0.5),
                        blurRadius: 36,
                        spreadRadius: 4,
                      ),
                    ],
                  ),
                  child: const Icon(
                    Icons.restaurant_rounded,
                    color: Colors.white,
                    size: 32,
                  ),
                ),
              ),
            ),
            const SizedBox(height: 36),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 52),
              child: AnimatedSwitcher(
                duration: const Duration(milliseconds: 420),
                transitionBuilder: (child, animation) => FadeTransition(
                  opacity: animation,
                  child: SlideTransition(
                    position: Tween<Offset>(
                      begin: const Offset(0, 0.10),
                      end: Offset.zero,
                    ).animate(
                        CurvedAnimation(parent: animation, curve: Curves.easeOut)),
                    child: child,
                  ),
                ),
                child: Text(
                  widget.phrase,
                  key: ValueKey(widget.phrase),
                  textAlign: TextAlign.center,
                  style: const TextStyle(
                    color: AppColors.textSecondary,
                    fontSize: 15,
                    height: 1.55,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

// Q&A flow
class _QuestionView extends StatefulWidget {
  final NutritionScanViewModel vm;
  const _QuestionView({required this.vm});

  @override
  State<_QuestionView> createState() => _QuestionViewState();
}

class _QuestionViewState extends State<_QuestionView>
    with SingleTickerProviderStateMixin {
  late AnimationController _anim;
  late Animation<Offset> _slide;
  late Animation<double> _fade;
  final _textController = TextEditingController();
  final _numberController = TextEditingController();
  String? _selectedOption;
  bool? _boolAnswer;

  @override
  void initState() {
    super.initState();
    _anim = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 380));
    _slide = Tween<Offset>(begin: const Offset(0.22, 0), end: Offset.zero)
        .animate(CurvedAnimation(parent: _anim, curve: Curves.easeOutCubic));
    _fade = CurvedAnimation(parent: _anim, curve: Curves.easeOut);
    _anim.forward();
  }

  @override
  void didUpdateWidget(_QuestionView old) {
    super.didUpdateWidget(old);
    if (widget.vm.currentQuestionIndex != old.vm.currentQuestionIndex) {
      _resetInputs();
      _anim.forward(from: 0);
    }
  }

  void _resetInputs() {
    _textController.clear();
    _numberController.clear();
    _selectedOption = null;
    _boolAnswer = null;
  }

  void _submit() {
    final q = widget.vm.currentQuestion;
    if (q == null) return;
    dynamic value;
    switch (q.inputType) {
      case 'select':
        if (_selectedOption == null) return;
        value = _selectedOption;
      case 'text':
        final t = _textController.text.trim();
        if (t.isEmpty) return;
        value = t;
      case 'number':
        final n = double.tryParse(_numberController.text.trim());
        if (n == null) return;
        value = n;
      case 'boolean':
        if (_boolAnswer == null) return;
        value = _boolAnswer;
      default:
        value = _textController.text.trim();
    }
    widget.vm.answerCurrent(value);
    setState(() => _resetInputs());
  }

  void _autoAdvance(dynamic value) {
    setState(() {
      if (value is bool) {
        _boolAnswer = value;
      } else {
        _selectedOption = value as String;
      }
    });
    Future.delayed(const Duration(milliseconds: 260), () {
      if (mounted) {
        widget.vm.answerCurrent(value);
        setState(() => _resetInputs());
      }
    });
  }

  @override
  void dispose() {
    _anim.dispose();
    _textController.dispose();
    _numberController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final vm = widget.vm;
    final q = vm.currentQuestion;
    if (q == null) return const SizedBox.shrink();

    final isAutoAdvance = q.inputType == 'select' || q.inputType == 'boolean';
    final topPad = MediaQuery.of(context).padding.top + kToolbarHeight + 20;
    final bottomPad = MediaQuery.of(context).padding.bottom + 16;

    return Padding(
      padding: EdgeInsets.fromLTRB(24, topPad, 24, bottomPad),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(
                'Q${vm.currentQuestionIndex + 1} of ${vm.questions.length}',
                style: const TextStyle(
                  color: AppColors.textTertiary,
                  fontSize: 12,
                  fontWeight: FontWeight.w500,
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(4),
                  child: LinearProgressIndicator(
                    value: vm.questionProgress,
                    backgroundColor: AppColors.glassWhiteFill,
                    valueColor:
                        const AlwaysStoppedAnimation(AppColors.accent),
                    minHeight: 3,
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 40),
          Expanded(
            child: FadeTransition(
              opacity: _fade,
              child: SlideTransition(
                position: _slide,
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      q.text,
                      style: const TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 22,
                        fontWeight: FontWeight.w700,
                        height: 1.35,
                      ),
                    ),
                    const SizedBox(height: 28),
                    _buildInput(q),
                  ],
                ),
              ),
            ),
          ),
          Row(
            children: [
              GestureDetector(
                onTap: vm.skipCurrent,
                behavior: HitTestBehavior.opaque,
                child: const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 4, vertical: 12),
                  child: Text(
                    'Skip',
                    style: TextStyle(
                        color: AppColors.textTertiary, fontSize: 14),
                  ),
                ),
              ),
              if (!isAutoAdvance) ...[
                const SizedBox(width: 16),
                Expanded(
                  child: GestureDetector(
                    onTap: _submit,
                    child: Container(
                      height: 52,
                      decoration: BoxDecoration(
                        gradient: LinearGradient(
                          begin: Alignment.topLeft,
                          end: Alignment.bottomRight,
                          colors: [AppColors.accent, AppColors.accentDark],
                        ),
                        borderRadius: BorderRadius.circular(14),
                        boxShadow: [
                          BoxShadow(
                            color: AppColors.accent.withValues(alpha: 0.38),
                            blurRadius: 18,
                            offset: const Offset(0, 5),
                          ),
                        ],
                      ),
                      child: Center(
                        child: Text(
                          vm.hasMoreQuestions ? 'Next' : 'Get Result',
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 16,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ],
          ),
        ],
      ),
    );
  }

  Widget _buildInput(ScanQuestion q) {
    return switch (q.inputType) {
      'select' => _SelectInput(
          options: q.options,
          selected: _selectedOption,
          onSelect: _autoAdvance,
        ),
      'number' => TextField(
          controller: _numberController,
          keyboardType: const TextInputType.numberWithOptions(decimal: true),
          style: const TextStyle(color: AppColors.textPrimary),
          decoration: _inputDecoration('Enter a number'),
        ),
      'boolean' => Row(
          children: [
            _BoolChip(
                label: 'Yes',
                selected: _boolAnswer == true,
                onTap: () => _autoAdvance(true)),
            const SizedBox(width: 12),
            _BoolChip(
                label: 'No',
                selected: _boolAnswer == false,
                onTap: () => _autoAdvance(false)),
          ],
        ),
      _ => TextField(
          controller: _textController,
          style: const TextStyle(color: AppColors.textPrimary),
          decoration: _inputDecoration('Type your answer'),
        ),
    };
  }

  InputDecoration _inputDecoration(String hint) => InputDecoration(
        hintText: hint,
        hintStyle: const TextStyle(color: AppColors.textTertiary),
        filled: true,
        fillColor: AppColors.glassWhiteFill,
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: AppColors.glassBorderDim),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide: const BorderSide(color: AppColors.glassBorderDim),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(12),
          borderSide:
              const BorderSide(color: AppColors.accent, width: 1.5),
        ),
      );
}

class _SelectInput extends StatelessWidget {
  final List<String> options;
  final String? selected;
  final void Function(String) onSelect;

  const _SelectInput(
      {required this.options,
      required this.selected,
      required this.onSelect});

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 10,
      runSpacing: 10,
      children: options.map((opt) {
        final active = selected == opt;
        return GestureDetector(
          onTap: () => onSelect(opt),
          child: AnimatedContainer(
            duration: const Duration(milliseconds: 180),
            curve: Curves.easeOutCubic,
            padding:
                const EdgeInsets.symmetric(horizontal: 18, vertical: 12),
            decoration: BoxDecoration(
              gradient: active
                  ? LinearGradient(colors: [
                      AppColors.accent.withValues(alpha: 0.28),
                      AppColors.accent.withValues(alpha: 0.12),
                    ])
                  : const LinearGradient(
                      colors: [Color(0x14FFFFFF), Color(0x08FFFFFF)]),
              borderRadius: BorderRadius.circular(30),
              border: Border.all(
                color: active
                    ? AppColors.accent.withValues(alpha: 0.70)
                    : AppColors.glassBorderDim,
                width: active ? 1.5 : 1,
              ),
              boxShadow: active
                  ? [
                      BoxShadow(
                          color: AppColors.accent.withValues(alpha: 0.22),
                          blurRadius: 12)
                    ]
                  : null,
            ),
            child: Text(
              opt,
              style: TextStyle(
                color: active ? AppColors.accentLight : AppColors.textPrimary,
                fontWeight:
                    active ? FontWeight.w700 : FontWeight.w400,
                fontSize: 14,
              ),
            ),
          ),
        );
      }).toList(),
    );
  }
}

class _BoolChip extends StatelessWidget {
  final String label;
  final bool selected;
  final VoidCallback onTap;

  const _BoolChip(
      {required this.label,
      required this.selected,
      required this.onTap});

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOutCubic,
        padding:
            const EdgeInsets.symmetric(horizontal: 36, vertical: 14),
        decoration: BoxDecoration(
          gradient: selected
              ? LinearGradient(colors: [
                  AppColors.accent.withValues(alpha: 0.28),
                  AppColors.accent.withValues(alpha: 0.12),
                ])
              : const LinearGradient(
                  colors: [Color(0x14FFFFFF), Color(0x08FFFFFF)]),
          borderRadius: BorderRadius.circular(30),
          border: Border.all(
            color: selected
                ? AppColors.accent.withValues(alpha: 0.70)
                : AppColors.glassBorderDim,
            width: selected ? 1.5 : 1,
          ),
          boxShadow: selected
              ? [
                  BoxShadow(
                      color: AppColors.accent.withValues(alpha: 0.22),
                      blurRadius: 12)
                ]
              : null,
        ),
        child: Text(
          label,
          style: TextStyle(
            color: selected ? AppColors.accentLight : AppColors.textPrimary,
            fontWeight: selected ? FontWeight.w700 : FontWeight.w400,
            fontSize: 15,
          ),
        ),
      ),
    );
  }
}

// Result
class _ResultView extends StatefulWidget {
  final NutritionAnalysisModel analysis;
  final VoidCallback onReset;

  const _ResultView({required this.analysis, required this.onReset});

  @override
  State<_ResultView> createState() => _ResultViewState();
}

class _ResultViewState extends State<_ResultView>
    with TickerProviderStateMixin {
  late final AnimationController _ctrl;

  late final Animation<double> _headerFade;
  late final Animation<Offset> _headerSlide;
  late final Animation<double> _verdictScale;
  late final Animation<double> _buddyFade;
  late final Animation<Offset> _buddySlide;
  late final Animation<double> _nutrientsFade;
  late final Animation<Offset> _nutrientsSlide;
  late final Animation<double> _prosFade;
  late final Animation<Offset> _prosSlide;
  late final Animation<double> _consFade;
  late final Animation<Offset> _consSlide;
  late final Animation<double> _tailFade;
  late final Animation<Offset> _tailSlide;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
        vsync: this, duration: const Duration(milliseconds: 1600));

    _headerFade = _f(0.00, 0.20);
    _headerSlide = _s(0.00, 0.20);
    _verdictScale = CurvedAnimation(
        parent: _ctrl,
        curve: const Interval(0.12, 0.36, curve: Curves.elasticOut));
    _buddyFade = _f(0.22, 0.42);
    _buddySlide = _s(0.22, 0.42);
    _nutrientsFade = _f(0.36, 0.55);
    _nutrientsSlide = _s(0.36, 0.55);
    _prosFade = _f(0.50, 0.68);
    _prosSlide = _s(0.50, 0.68, dir: const Offset(-0.10, 0));
    _consFade = _f(0.58, 0.76);
    _consSlide = _s(0.58, 0.76, dir: const Offset(0.10, 0));
    _tailFade = _f(0.70, 0.90);
    _tailSlide = _s(0.70, 0.90);

    _ctrl.forward();
  }

  Animation<double> _f(double s, double e) => CurvedAnimation(
      parent: _ctrl, curve: Interval(s, e, curve: Curves.easeOut));

  Animation<Offset> _s(double s, double e,
          {Offset dir = const Offset(0, 0.10)}) =>
      Tween<Offset>(begin: dir, end: Offset.zero).animate(
          CurvedAnimation(
              parent: _ctrl, curve: Interval(s, e, curve: Curves.easeOut)));

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  Color get _verdictColor => switch (widget.analysis.recommendation) {
        'eat' => AppColors.success,
        'skip' => AppColors.error,
        _ => AppColors.warning,
      };

  String get _verdictLabel => switch (widget.analysis.recommendation) {
        'eat' => 'EAT',
        'skip' => 'SKIP',
        _ => 'MODERATE',
      };

  @override
  Widget build(BuildContext context) {
    final a = widget.analysis;
    final topPad =
        MediaQuery.of(context).padding.top + kToolbarHeight + 8;
    final bottomPad =
        MediaQuery.of(context).padding.bottom + 32;

    return SingleChildScrollView(
      padding: EdgeInsets.fromLTRB(20, topPad, 20, bottomPad),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Header
          FadeTransition(
            opacity: _headerFade,
            child: SlideTransition(
              position: _headerSlide,
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          a.foodName,
                          style: const TextStyle(
                            color: AppColors.textPrimary,
                            fontSize: 24,
                            fontWeight: FontWeight.w800,
                            height: 1.2,
                            letterSpacing: -0.5,
                          ),
                        ),
                        if (a.headline.isNotEmpty) ...[
                          const SizedBox(height: 7),
                          Text(
                            '"${a.headline}"',
                            style: const TextStyle(
                              color: AppColors.textSecondary,
                              fontSize: 14,
                              fontStyle: FontStyle.italic,
                              height: 1.5,
                            ),
                          ),
                        ],
                      ],
                    ),
                  ),
                  const SizedBox(width: 14),
                  ScaleTransition(
                    scale: _verdictScale,
                    child: _VerdictBadge(
                        label: _verdictLabel, color: _verdictColor),
                  ),
                ],
              ),
            ),
          ),

          const SizedBox(height: 22),

          // Buddy's take
          FadeTransition(
            opacity: _buddyFade,
            child: SlideTransition(
              position: _buddySlide,
              child: _BuddyTakeCard(
                  verdictReason: a.verdictReason,
                  accentColor: _verdictColor),
            ),
          ),

          // Key nutrients
          if (a.keyNutrients.isNotEmpty) ...[
            const SizedBox(height: 22),
            FadeTransition(
              opacity: _nutrientsFade,
              child: SlideTransition(
                position: _nutrientsSlide,
                child: _KeyNutrientsRow(nutrients: a.keyNutrients),
              ),
            ),
          ],

          // Pros
          if (a.pros.isNotEmpty) ...[
            const SizedBox(height: 22),
            FadeTransition(
              opacity: _prosFade,
              child: SlideTransition(
                position: _prosSlide,
                child: _SignalSection(
                  label: 'What works for you',
                  items: a.pros,
                  accentColor: AppColors.success,
                  bullet: '↑',
                ),
              ),
            ),
          ],

          // Cons
          if (a.cons.isNotEmpty) ...[
            const SizedBox(height: 12),
            FadeTransition(
              opacity: _consFade,
              child: SlideTransition(
                position: _consSlide,
                child: _SignalSection(
                  label: 'Keep in mind',
                  items: a.cons,
                  accentColor: AppColors.warning,
                  bullet: '↓',
                ),
              ),
            ),
          ],

          // Concerns
          if (a.concerns.isNotEmpty) ...[
            const SizedBox(height: 12),
            FadeTransition(
              opacity: _consFade,
              child: SlideTransition(
                position: _consSlide,
                child: _SignalSection(
                  label: 'Heads up',
                  items: a.concerns,
                  accentColor: AppColors.error,
                  bullet: '!',
                ),
              ),
            ),
          ],

          // All the numbers
          const SizedBox(height: 22),
          FadeTransition(
            opacity: _tailFade,
            child: SlideTransition(
              position: _tailSlide,
              child: _FullMacrosCollapsible(macros: a.macros),
            ),
          ),

          // Scan Another
          const SizedBox(height: 28),
          FadeTransition(
            opacity: _tailFade,
            child: GestureDetector(
              onTap: widget.onReset,
              child: FauxGlassCard(
                borderRadius: 14,
                borderColor: AppColors.glassBorderLight,
                padding: const EdgeInsets.symmetric(vertical: 16),
                child: const Row(
                  mainAxisAlignment: MainAxisAlignment.center,
                  children: [
                    Icon(Icons.camera_alt_rounded,
                        color: AppColors.textTertiary, size: 18),
                    SizedBox(width: 8),
                    Text(
                      'Scan Another',
                      style: TextStyle(
                        color: AppColors.textPrimary,
                        fontSize: 15,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _VerdictBadge extends StatelessWidget {
  final String label;
  final Color color;
  const _VerdictBadge({required this.label, required this.color});

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            color.withValues(alpha: 0.22),
            color.withValues(alpha: 0.08),
          ],
        ),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: color.withValues(alpha: 0.55), width: 1.5),
        boxShadow: [
          BoxShadow(
            color: color.withValues(alpha: 0.32),
            blurRadius: 20,
            spreadRadius: 0,
          ),
        ],
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 12,
          fontWeight: FontWeight.w800,
          letterSpacing: 1.2,
        ),
      ),
    );
  }
}

class _BuddyTakeCard extends StatelessWidget {
  final String verdictReason;
  final Color accentColor;

  const _BuddyTakeCard(
      {required this.verdictReason, required this.accentColor});

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0x14FFFFFF), Color(0x08FFFFFF)],
        ),
        borderRadius: BorderRadius.circular(16),
        border: Border(
          left: BorderSide(color: accentColor, width: 3),
          top: BorderSide(
              color: AppColors.glassBorderDim, width: 1),
          right: BorderSide(
              color: AppColors.glassBorderDim, width: 1),
          bottom: BorderSide(
              color: AppColors.glassBorderDim, width: 1),
        ),
      ),
      padding: const EdgeInsets.fromLTRB(18, 16, 16, 16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(
              color: accentColor.withValues(alpha: 0.15),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(
              "Buddy's take",
              style: TextStyle(
                color: accentColor,
                fontSize: 10,
                fontWeight: FontWeight.w800,
                letterSpacing: 0.5,
              ),
            ),
          ),
          const SizedBox(height: 10),
          Text(
            verdictReason,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 14,
              height: 1.65,
            ),
          ),
        ],
      ),
    );
  }
}

class _KeyNutrientsRow extends StatelessWidget {
  final List<KeyNutrient> nutrients;
  const _KeyNutrientsRow({required this.nutrients});

  Color _colorFor(String sentiment) => switch (sentiment) {
        'good' => AppColors.success,
        'watch' => AppColors.warning,
        _ => AppColors.textTertiary,
      };

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'KEY NUTRIENTS',
          style: TextStyle(
            color: AppColors.textTertiary,
            fontSize: 11,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.6,
          ),
        ),
        const SizedBox(height: 10),
        SizedBox(
          height: 120,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: nutrients.length,
            separatorBuilder: (_, _) => const SizedBox(width: 10),
            itemBuilder: (_, i) => _NutrientCard(
              nutrient: nutrients[i],
              accentColor: _colorFor(nutrients[i].sentiment),
            ),
          ),
        ),
      ],
    );
  }
}

class _NutrientCard extends StatelessWidget {
  final KeyNutrient nutrient;
  final Color accentColor;

  const _NutrientCard(
      {required this.nutrient, required this.accentColor});

  @override
  Widget build(BuildContext context) {
    return Container(
      width: 148,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            accentColor.withValues(alpha: 0.14),
            accentColor.withValues(alpha: 0.05),
          ],
        ),
        borderRadius: BorderRadius.circular(16),
        border:
            Border.all(color: accentColor.withValues(alpha: 0.28), width: 1),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
            decoration: BoxDecoration(
              color: accentColor.withValues(alpha: 0.16),
              borderRadius: BorderRadius.circular(5),
            ),
            child: Text(
              nutrient.name.toUpperCase(),
              style: TextStyle(
                color: accentColor,
                fontSize: 9,
                fontWeight: FontWeight.w800,
                letterSpacing: 0.5,
              ),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          Text(
            nutrient.value,
            style: const TextStyle(
              color: AppColors.textPrimary,
              fontSize: 22,
              fontWeight: FontWeight.w800,
              height: 1.1,
            ),
          ),
          Text(
            nutrient.context,
            style: const TextStyle(
              color: AppColors.textSecondary,
              fontSize: 11,
              height: 1.35,
            ),
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
          ),
        ],
      ),
    );
  }
}

class _SignalSection extends StatelessWidget {
  final String label;
  final List<String> items;
  final Color accentColor;
  final String bullet;

  const _SignalSection({
    required this.label,
    required this.items,
    required this.accentColor,
    required this.bullet,
  });

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard(
      borderRadius: 14,
      borderColor: accentColor.withValues(alpha: 0.25),
      gradient: LinearGradient(
        begin: Alignment.topLeft,
        end: Alignment.bottomRight,
        colors: [
          accentColor.withValues(alpha: 0.10),
          accentColor.withValues(alpha: 0.04),
        ],
      ),
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding:
                const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
            decoration: BoxDecoration(
              color: accentColor.withValues(alpha: 0.16),
              borderRadius: BorderRadius.circular(6),
            ),
            child: Text(
              label.toUpperCase(),
              style: TextStyle(
                color: accentColor,
                fontSize: 10,
                fontWeight: FontWeight.w800,
                letterSpacing: 0.5,
              ),
            ),
          ),
          const SizedBox(height: 12),
          ...items.map(
            (item) => Padding(
              padding: const EdgeInsets.only(bottom: 9),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    width: 20,
                    height: 20,
                    margin: const EdgeInsets.only(top: 1),
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: accentColor.withValues(alpha: 0.15),
                    ),
                    child: Center(
                      child: Text(
                        bullet,
                        style: TextStyle(
                          color: accentColor,
                          fontSize: 11,
                          fontWeight: FontWeight.w800,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      item,
                      style: const TextStyle(
                        color: AppColors.textSecondary,
                        fontSize: 13,
                        height: 1.5,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _FullMacrosCollapsible extends StatefulWidget {
  final Map<String, double> macros;
  const _FullMacrosCollapsible({required this.macros});

  @override
  State<_FullMacrosCollapsible> createState() =>
      _FullMacrosCollapsibleState();
}

class _FullMacrosCollapsibleState extends State<_FullMacrosCollapsible> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    return FauxGlassCard(
      borderRadius: 14,
      borderColor: AppColors.glassBorderDim,
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          GestureDetector(
            onTap: () => setState(() => _expanded = !_expanded),
            behavior: HitTestBehavior.opaque,
            child: Row(
              children: [
                const Text(
                  'ALL THE NUMBERS',
                  style: TextStyle(
                    color: AppColors.textTertiary,
                    fontSize: 11,
                    fontWeight: FontWeight.w700,
                    letterSpacing: 0.6,
                  ),
                ),
                const Spacer(),
                AnimatedRotation(
                  turns: _expanded ? 0.5 : 0,
                  duration: const Duration(milliseconds: 260),
                  child: const Icon(
                    Icons.keyboard_arrow_down_rounded,
                    color: AppColors.textTertiary,
                    size: 20,
                  ),
                ),
              ],
            ),
          ),
          AnimatedCrossFade(
            firstChild: const SizedBox.shrink(),
            secondChild: Padding(
              padding: const EdgeInsets.only(top: 14),
              child: _MacroTable(macros: widget.macros),
            ),
            crossFadeState: _expanded
                ? CrossFadeState.showSecond
                : CrossFadeState.showFirst,
            duration: const Duration(milliseconds: 260),
          ),
        ],
      ),
    );
  }
}

class _MacroTable extends StatelessWidget {
  final Map<String, double> macros;
  const _MacroTable({required this.macros});

  @override
  Widget build(BuildContext context) {
    final rows = [
      ('Calories', '${macros['calories']?.toStringAsFixed(0) ?? '—'} kcal'),
      ('Protein', '${macros['protein_g']?.toStringAsFixed(1) ?? '—'} g'),
      ('Carbs', '${macros['carbs_g']?.toStringAsFixed(1) ?? '—'} g'),
      ('Fat', '${macros['fat_g']?.toStringAsFixed(1) ?? '—'} g'),
      ('Sugar', '${macros['sugar_g']?.toStringAsFixed(1) ?? '—'} g'),
      ('Fiber', '${macros['fiber_g']?.toStringAsFixed(1) ?? '—'} g'),
      ('Sodium', '${macros['sodium_mg']?.toStringAsFixed(0) ?? '—'} mg'),
    ];

    return Column(
      children: rows.asMap().entries.map((e) {
        final isLast = e.key == rows.length - 1;
        return Container(
          padding: const EdgeInsets.symmetric(vertical: 11),
          decoration: BoxDecoration(
            border: isLast
                ? null
                : const Border(
                    bottom: BorderSide(
                        color: AppColors.glassBorderDim, width: 1)),
          ),
          child: Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                e.value.$1,
                style: const TextStyle(
                    color: AppColors.textTertiary, fontSize: 13),
              ),
              Text(
                e.value.$2,
                style: const TextStyle(
                  color: AppColors.textPrimary,
                  fontSize: 13,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
        );
      }).toList(),
    );
  }
}

// Error view
class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;

  const _ErrorView({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    final topPad = MediaQuery.of(context).padding.top + kToolbarHeight;
    return Padding(
      padding: EdgeInsets.only(top: topPad),
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Container(
                width: 72,
                height: 72,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: AppColors.error.withValues(alpha: 0.12),
                  border: Border.all(
                      color: AppColors.error.withValues(alpha: 0.30),
                      width: 1),
                ),
                child: const Icon(Icons.error_outline_rounded,
                    color: AppColors.error, size: 32),
              ),
              const SizedBox(height: 20),
              Text(
                message,
                textAlign: TextAlign.center,
                style: const TextStyle(
                    color: AppColors.textSecondary,
                    fontSize: 15,
                    height: 1.5),
              ),
              const SizedBox(height: 28),
              GestureDetector(
                onTap: onRetry,
                child: Container(
                  padding: const EdgeInsets.symmetric(
                      horizontal: 32, vertical: 14),
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                        colors: [AppColors.accent, AppColors.accentDark]),
                    borderRadius: BorderRadius.circular(14),
                    boxShadow: [
                      BoxShadow(
                        color: AppColors.accent.withValues(alpha: 0.38),
                        blurRadius: 18,
                        offset: const Offset(0, 5),
                      ),
                    ],
                  ),
                  child: const Text(
                    'Try Again',
                    style: TextStyle(
                        color: Colors.white,
                        fontSize: 15,
                        fontWeight: FontWeight.w700),
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

// Profile sheet
class _ProfileSheet extends StatelessWidget {
  final DietaryProfileModel? profile;
  const _ProfileSheet({required this.profile});

  @override
  Widget build(BuildContext context) {
    final p = profile;
    return Container(
      decoration: const BoxDecoration(
        color: AppColors.deepBackground,
        borderRadius: BorderRadius.vertical(top: Radius.circular(28)),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Center(
            child: Padding(
              padding: const EdgeInsets.only(top: 12, bottom: 20),
              child: Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: AppColors.glassBorderLight,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 0, 20, 0),
            child: const Text(
              'Your Profile',
              style: TextStyle(
                color: AppColors.textPrimary,
                fontSize: 20,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          const SizedBox(height: 16),
          if (p == null)
            Padding(
              padding: EdgeInsets.fromLTRB(
                  20, 0, 20, MediaQuery.of(context).padding.bottom + 24),
              child: FauxGlassCard(
                borderRadius: 16,
                borderColor: AppColors.glassBorderDim,
                padding: const EdgeInsets.all(24),
                child: const Column(
                  children: [
                    Icon(Icons.person_outline_rounded,
                        color: AppColors.textTertiary, size: 40),
                    SizedBox(height: 12),
                    Text(
                      'No dietary profile set up yet.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: AppColors.textSecondary, fontSize: 14),
                    ),
                    SizedBox(height: 4),
                    Text(
                      'Enable Nutrition Agent in the Agents tab.',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                          color: AppColors.textTertiary, fontSize: 13),
                    ),
                  ],
                ),
              ),
            )
          else ...[
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20),
              child: _buildProfileRows(p),
            ),
            SizedBox(
                height: MediaQuery.of(context).padding.bottom + 24),
          ],
        ],
      ),
    );
  }

  Widget _buildProfileRows(DietaryProfileModel p) {
    final rows = <(String, String)>[
      if (p.goal != null) ('Goal', p.goal!),
      if (p.activityLevel != null) ('Activity', p.activityLevel!),
      if (p.restrictions.isNotEmpty)
        ('Diet', p.restrictions.join(', ')),
      if (p.allergies.isNotEmpty) ('Allergies', p.allergies.join(', ')),
      if (p.weightKg != null)
        ('Weight', '${p.weightKg!.toStringAsFixed(1)} kg'),
    ];

    if (rows.isEmpty) {
      return const Text(
        'No details on file.',
        style: TextStyle(color: AppColors.textTertiary, fontSize: 13),
      );
    }

    return FauxGlassCard(
      borderRadius: 16,
      borderColor: AppColors.glassBorderDim,
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Column(
        children: rows.asMap().entries.map((e) {
          final isLast = e.key == rows.length - 1;
          return Container(
            padding: const EdgeInsets.symmetric(
                horizontal: 16, vertical: 13),
            decoration: BoxDecoration(
              border: isLast
                  ? null
                  : const Border(
                      bottom: BorderSide(
                          color: AppColors.glassBorderDim, width: 1)),
            ),
            child: Row(
              children: [
                SizedBox(
                  width: 80,
                  child: Text(
                    e.value.$1,
                    style: const TextStyle(
                        color: AppColors.textTertiary, fontSize: 13),
                  ),
                ),
                Expanded(
                  child: Text(
                    e.value.$2,
                    style: const TextStyle(
                      color: AppColors.textPrimary,
                      fontSize: 13,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
              ],
            ),
          );
        }).toList(),
      ),
    );
  }
}
