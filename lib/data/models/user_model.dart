class UserSettings {
  final bool wakeWordEnabled;
  final bool ttsEnabled;

  const UserSettings({
    required this.wakeWordEnabled,
    required this.ttsEnabled,
  });

  factory UserSettings.defaults() {
    return const UserSettings(
      wakeWordEnabled: false,
      ttsEnabled: true,
    );
  }

  factory UserSettings.fromJson(Map<String, dynamic> json) {
    return UserSettings(
      wakeWordEnabled: json['wake_word_enabled'] as bool? ?? false,
      ttsEnabled: json['tts_enabled'] as bool? ?? true,
    );
  }

  Map<String, dynamic> toJson() => {
        'wake_word_enabled': wakeWordEnabled,
        'tts_enabled': ttsEnabled,
      };

  UserSettings copyWith({
    bool? wakeWordEnabled,
    bool? ttsEnabled,
  }) {
    return UserSettings(
      wakeWordEnabled: wakeWordEnabled ?? this.wakeWordEnabled,
      ttsEnabled: ttsEnabled ?? this.ttsEnabled,
    );
  }
}

class UserModel {
  static const String fieldLastLoginAt = 'last_login_at';
  static const String fieldLoginCount = 'login_count';
  static const String fieldLastLogoutAt = 'last_logout_at';
  static const String fieldLogoutCount = 'logout_count';
  static const String fieldIsActive = 'is_active';
  static const String fieldSignInMethod = 'sign_in_method';
  static const String fieldPlatform = 'platform';
  // Denormalized surface footprint. `platform` above is the single signup device;
  // this array accumulates every surface the account touches (this phone plus any
  // linked desktop/web), written with an atomic array-union by each surface so it
  // converges to e.g. ["android", "windows"]. The backend writers own the desktop
  // entries (handlers/pairing.py, handlers/web_auth.py).
  static const String fieldLinkedPlatforms = 'linked_platforms';

  final String uid;
  final String displayName;
  final String email;
  final String? photoUrl;
  final UserSettings settings;
  final DateTime createdAt;
  final DateTime lastActiveAt;
  final String? timezone;
  final bool onboardingComplete;
  final bool? auraConsentGranted;
  final String? dateOfBirth;
  final DateTime? lastLoginAt;
  final int loginCount;
  final DateTime? lastLogoutAt;
  final int logoutCount;
  final bool isActive;
  final String? signInMethod;
  final String? platform;

  const UserModel({
    required this.uid,
    required this.displayName,
    required this.email,
    this.photoUrl,
    required this.settings,
    required this.createdAt,
    required this.lastActiveAt,
    this.timezone,
    this.onboardingComplete = true,
    this.auraConsentGranted,
    this.dateOfBirth,
    this.lastLoginAt,
    this.loginCount = 0,
    this.lastLogoutAt,
    this.logoutCount = 0,
    this.isActive = false,
    this.signInMethod,
    this.platform,
  });

  factory UserModel.fromJson(Map<String, dynamic> json) {
    return UserModel(
      uid: json['uid'] as String,
      displayName: json['display_name'] as String? ?? '',
      email: json['email'] as String,
      photoUrl: json['photo_url'] as String?,
      settings: UserSettings.fromJson(
        json['settings'] as Map<String, dynamic>? ?? {},
      ),
      createdAt: DateTime.parse(json['created_at'] as String),
      lastActiveAt: DateTime.parse(json['last_active_at'] as String),
      timezone: json['timezone'] as String?,
      onboardingComplete: json['onboarding_complete'] as bool? ?? true, // Existing users without this field are treated as onboarded.
      auraConsentGranted: json['aura_consent_granted'] as bool?,
      dateOfBirth: json['date_of_birth'] as String?,
      lastLoginAt: _parseIso(json[fieldLastLoginAt]),
      loginCount: (json[fieldLoginCount] as num?)?.toInt() ?? 0,
      lastLogoutAt: _parseIso(json[fieldLastLogoutAt]),
      logoutCount: (json[fieldLogoutCount] as num?)?.toInt() ?? 0,
      isActive: json[fieldIsActive] as bool? ?? false,
      signInMethod: json[fieldSignInMethod] as String?,
      platform: json[fieldPlatform] as String?,
    );
  }

  static DateTime? _parseIso(Object? value) {
    if (value is! String || value.isEmpty) return null;
    return DateTime.tryParse(value);
  }

  Map<String, dynamic> toJson() => {
        'uid': uid,
        'display_name': displayName,
        'email': email,
        'photo_url': photoUrl,
        'settings': settings.toJson(),
        'created_at': createdAt.toUtc().toIso8601String(),
        'last_active_at': lastActiveAt.toUtc().toIso8601String(),
        'timezone': timezone,
        'onboarding_complete': onboardingComplete,
        'aura_consent_granted': auraConsentGranted,
        'date_of_birth': dateOfBirth,
      };

  UserModel copyWith({
    String? displayName,
    String? email,
    String? photoUrl,
    UserSettings? settings,
    DateTime? lastActiveAt,
    String? timezone,
    bool? onboardingComplete,
    bool? auraConsentGranted,
    String? dateOfBirth,
  }) {
    return UserModel(
      uid: uid,
      displayName: displayName ?? this.displayName,
      email: email ?? this.email,
      photoUrl: photoUrl ?? this.photoUrl,
      settings: settings ?? this.settings,
      createdAt: createdAt,
      lastActiveAt: lastActiveAt ?? this.lastActiveAt,
      timezone: timezone ?? this.timezone,
      onboardingComplete: onboardingComplete ?? this.onboardingComplete,
      auraConsentGranted: auraConsentGranted ?? this.auraConsentGranted,
      dateOfBirth: dateOfBirth ?? this.dateOfBirth,
      lastLoginAt: lastLoginAt,
      loginCount: loginCount,
      lastLogoutAt: lastLogoutAt,
      logoutCount: logoutCount,
      isActive: isActive,
      signInMethod: signInMethod,
      platform: platform,
    );
  }
}
