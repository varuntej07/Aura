/// The interest categories offered in onboarding.
///
/// This list MUST mirror the backend's producible set
/// (`ONBOARDABLE_CATEGORIES` in `backend/src/services/signal_engine/content_category_map.py`). 
/// Offering a slug the backend can't satisfy would silently never surface content; 
/// offering a slug the backend doesn't recognise would be dropped server-side. 
/// The contract test `backend/tests/test_onboarding_interests_contract.py` reads this file and fails
/// CI if the slug set drifts from the backend, a rename on either side breaks the
/// build instead of silently muting a declared interest.
///
/// Labels are display-only and may differ from the backend's labels; 
/// only the slug set is contractual.
class OnboardableInterest {
  final String slug;
  final String label;
  const OnboardableInterest(this.slug, this.label);
}

class OnboardableInterests {
  OnboardableInterests._();

  static const List<OnboardableInterest> all = [
    OnboardableInterest('entertainment_media', 'Entertainment'),
    OnboardableInterest('sports', 'Sports'),
    OnboardableInterest('news_current_affairs', 'News'),
    OnboardableInterest('technology_computing', 'Technology'),
    OnboardableInterest('business_economy', 'Business'),
    OnboardableInterest('health_medical', 'Health'),
    OnboardableInterest('science_nature', 'Science'),
    OnboardableInterest('regional_local_affairs', 'Local & Regional'),
  ];

  /// Minimum picks required to continue onboarding.
  static const int minSelection = 3;

  static List<String> get slugs => all.map((e) => e.slug).toList();
}
